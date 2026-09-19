"""Deterministic timing and capture regression tests; no hardware access."""
import contextlib
import io
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_pc_tools import ack, config, session
import pc_update as u


class TimingTests(unittest.TestCase):
    def test_query_completion_does_not_claim_device_success(self):
        updater, bus, log, clock = session(config(completion_criterion='status_query_sent'))
        bus.responses[0x46D0] = []
        self.assertEqual(updater.run(bytes(128)), 'status_query_sent')
        self.assertEqual(sum(i == 0x46D0 for i, _ in bus.sent), 1)
        self.assertTrue(any('device_result=unconfirmed' in s for s in log.lines))
        self.assertFalse(any('Update successful' in s for s in log.lines))

    def test_query_send_failure_is_not_completion(self):
        updater, bus, log, clock = session(config(completion_criterion='status_query_sent'))
        original = bus.send

        def send(identifier, data):
            if identifier == 0x46D0:
                raise OSError('send failed')
            original(identifier, data)

        bus.send = send
        with self.assertRaises(OSError):
            updater.run(bytes(128))
        self.assertFalse(any('completion=status_query_sent' in s for s in log.lines))

    def test_block_logging_and_preparation_share_ack_delay(self):
        for log_delay in (0.020, 0.150):
            with self.subTest(log_delay=log_delay):
                updater, bus, log, clock = session(config(
                    stop_after='transfer', frame_interval=0.002, block_ack_delay=0.075))
                sends, batches = [], []
                original_send = bus.send
                original_batch = log.batch_frames

                def send(identifier, data):
                    sends.append((identifier, clock()))
                    original_send(identifier, data)

                def batch(frames, interface=''):
                    batches.append((len(bus.sent), frames[-1]['monotonic']))
                    original_batch(frames, interface)
                    clock.sleep(log_delay)

                bus.send, log.batch_frames = send, batch
                updater.run(bytes(256))
                self.assertEqual([n for n, _ in batches], [19, 37])
                first, second = sends[1:19], sends[19:37]
                for packet in (first, second):
                    for a, b in zip(packet, packet[1:]):
                        self.assertAlmostEqual(b[1] - a[1], 0.002)
                self.assertAlmostEqual(second[0][1] - batches[0][1], max(0.075, log_delay))
                self.assertEqual(len([s for s in log.lines if s.startswith('BLOCK_TIMING')]), 2)

    def test_scheduler_delay_does_not_cause_catchup_burst(self):
        updater, bus, log, clock = session(config(stop_after='transfer', frame_interval=0.002))
        times = []
        original_send = bus.send
        original_sleep = clock.sleep
        calls = []

        def send(identifier, data):
            if identifier in (0x4630, 0x4650, 0x4670):
                times.append(clock())
            original_send(identifier, data)

        def sleep(seconds):
            calls.append(seconds)
            original_sleep(seconds + (0.050 if len(times) == 5 else 0))

        bus.send, updater.sleep = send, sleep
        updater.run(bytes(128))
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreater(max(gaps), 0.05)
        self.assertTrue(all(g >= 0.002 - 1e-9 for g in gaps))

    def test_timeout_keeps_deadline_and_flushes_partial_evidence(self):
        updater, bus, log, clock = session(config(stop_after='transfer', ack_timeout=0.1),
                                          {0x4610: [ack(0x4621, [0xA1, 0, 128])]})
        original_batch = log.batch_frames
        flushed_at = []

        def batch(frames, interface=''):
            flushed_at.append(clock())
            original_batch(frames, interface)
            clock.sleep(1)

        log.batch_frames = batch
        with self.assertRaises(TimeoutError):
            updater.run(bytes(128))
        trailer = next(f for f in log.frames if f['id'] == 0x4670)
        self.assertAlmostEqual(flushed_at[0] - trailer['monotonic'], 0.1)
        self.assertIsNone(updater.events)
        self.assertTrue(any('last_confirmed=0' in s for s in log.lines))

    def test_interrupt_flushes_frames_already_sent(self):
        updater, bus, log, clock = session(config(stop_after='transfer'))
        original = bus.send

        def send(identifier, data):
            if identifier == 0x4650:
                raise KeyboardInterrupt()
            original(identifier, data)

        bus.send = send
        with self.assertRaises(KeyboardInterrupt):
            updater.run(bytes(128))
        self.assertTrue(any(f['id'] == 0x4630 for f in log.frames))
        self.assertTrue(any('submitted=1' in s for s in log.lines))
        self.assertIsNone(updater.events)

    def test_logger_preserves_event_timestamp_and_batches_flush(self):
        logger = u.Logger(quiet_frames=True)
        logger.file = io.StringIO()
        frames = [dict(ack(0x4681, [0xA2]), wall_time=1.25, monotonic=2.5)]
        with patch.object(logger.file, 'flush', wraps=logger.file.flush) as flush, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            logger.batch_frames(frames * 3)
            self.assertEqual(flush.call_count, 1)
            self.assertEqual(output.getvalue(), '')
        text = logger.file.getvalue()
        self.assertEqual(text.count('mono=2.5'), 3)
        self.assertEqual(text.count('.250 '), 3)

    def test_missing_diagnostics_are_nonfatal(self):
        updater, bus, log, clock = session()
        for exc in (FileNotFoundError('ip'), subprocess.TimeoutExpired('ip', 3)):
            with patch.object(u.subprocess, 'run', side_effect=exc):
                u.can_diagnostic(updater.config, log, 'failed')
        self.assertEqual(sum('unavailable' in s for s in log.lines), 2)

    def test_full_vendor_capture_is_unchanged(self):
        path = Path(__file__).resolve().parents[1] / 'logs/BMS Upgrade All.txt'
        packets, payload, size, wire_crc = [], bytearray(), None, None
        for ident, data in re.findall(r'ID=0x([0-9A-Fa-f]+), Data=([0-9A-Fa-f ]+)', path.read_text()):
            ident, data = int(ident, 16), bytes.fromhex(data)
            if ident == 0x4610:
                size = int.from_bytes(data[:4], 'little')
            elif ident == 0x4630:
                packets.append([])
            elif ident == 0x4650:
                payload.extend(data)
            elif ident == 0x4690:
                wire_crc = data
            if ident in (0x4630, 0x4650, 0x4670):
                packets[-1].append((ident, data))
        cfg = u.parser().parse_args([])
        firmware = bytes(payload[:size])
        generated = [[(i, u.request_payload(i, d, cfg)) for i, d in frames]
                     for _, _, frames in u.blocks(firmware, cfg)]
        self.assertEqual(len(generated), 2179)
        self.assertEqual(generated, packets)
        self.assertEqual(u.request_payload(0x4690, u.firmware_crc(firmware, cfg), cfg), wire_crc)


if __name__ == '__main__':
    unittest.main()
