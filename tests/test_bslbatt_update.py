"""Standalone Venus updater regression tests, without CAN hardware."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch
import xml.etree.ElementTree as ET
import zipfile

from test_pc_tools import Bus, Clock, Log, ack, session
import pc_update as reference

spec = importlib.util.spec_from_file_location('bslbatt_tool', Path(__file__).resolve().parents[1] / 'bslbatt-tool.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class ProtocolTests(unittest.TestCase):
    def make_session(self, responses=None):
        original, bus, log, clock = session(reference.parser().parse_args([]), responses)
        updater = u.BslbattFirmwareUpdater(bus, SimpleNamespace(can='can0', **u.UPGRADE_CONFIG), log, clock, clock.sleep)
        return updater, bus, log, clock

    def test_wire_and_timing_match_validated_script(self):
        for size in (1, 127, 128, 129, 256, 300):
            with self.subTest(size=size), contextlib.redirect_stdout(io.StringIO()) as output:
                data = bytes(i % 256 for i in range(size))
                reference_config = reference.parser().parse_args([])
                reference_config.frame_interval = 0.002
                reference_config.block_ack_delay = 0.047
                original, old_bus, old_log, old_clock = session(reference_config)
                original.run(data)
                updater, bus, log, clock = self.make_session()
                self.assertEqual(updater.run(data), 'status_query_sent')
                self.assertEqual(bus.sent, old_bus.sent)
                self.assertEqual(clock.now, old_clock.now)
                self.assertEqual([(f['id'], f['monotonic']) for f in log.frames],
                                 [(f['id'], f['monotonic']) for f in old_log.frames])
                self.assertTrue(all(len(data) == 8 for _, data in bus.sent))
                self.assertEqual(sum(identifier == 0x46D0 for identifier, _ in bus.sent), 1)
                self.assertIn('device final status unconfirmed', output.getvalue())
                ET.fromstring('<output>' + output.getvalue() + '</output>')

    def test_status_ack_extra_byte_and_padding(self):
        for module in (u, reference):
            with self.subTest(module=module.__name__):
                payload = bytes.fromhex('0D 01 00 00 00 00 00 00')
                self.assertEqual(module.response_payload(0x46E1, payload), b'\x0d\x01')
                self.assertIn('status=0x0D', module.decode(0x46E1, payload))
                self.assertIn('extra=01', module.decode(0x46E1, payload))
                for invalid in (b'', b'\x0d\x01\x00',
                                bytes.fromhex('0D 01 02 00 00 00 00 00')):
                    with self.assertRaises(ValueError):
                        module.response_payload(0x46E1, invalid)
                # The extra byte is specific to STATUS_ACK, not other ACKs.
                with self.assertRaises(ValueError):
                    module.response_payload(0x4681, bytes.fromhex('A2 01 00 00 00 00 00 00'))

    def test_all_protocol_errors_stop_before_query(self):
        for request, response, code in ((0x4610, 0x4621, 1), (0x4670, 0x4681, 2),
                                       (0x4670, 0x4681, 3), (0x4690, 0x46A1, 8),
                                       (0x46B0, 0x46C1, 9)):
            with self.subTest(code=code), contextlib.redirect_stdout(io.StringIO()):
                updater, bus, _, _ = self.make_session()
                bus.responses[request] = [ack(response, [code])]
                with self.assertRaises(u.ProtocolError) as error:
                    updater.run(bytes(129))
                self.assertEqual(error.exception.code, code)
                self.assertEqual(sum(i == 0x4610 for i, _ in bus.sent), 1)
                self.assertNotIn(0x46D0, [i for i, _ in bus.sent])

    def test_timeout_ignores_invalid_frames_without_extending_deadline(self):
        updater, bus, _, clock = self.make_session()
        bus.responses[0x4610] = [ack(0x4621, [0xA1, 0, 128], extended=False),
                                 ack(0x4621, [0xA1, 0, 128, 1, 0, 0, 0, 0])]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(TimeoutError):
            updater.run(b'x')
        self.assertAlmostEqual(clock.now, 300)
        self.assertEqual(len(bus.sent), 1)

    def test_block_without_success_ack_never_sends_next_block(self):
        for responses, error in (([], TimeoutError),
                                 ([ack(0x4681, [2])], u.ProtocolError)):
            with self.subTest(responses=responses):
                updater, bus, log, clock = self.make_session()
                bus.responses[0x4670] = responses
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(error):
                    updater.run(bytes(256))
                self.assertEqual([i for i, _ in bus.sent],
                                 [0x4610, 0x4630] + [0x4650] * 16 + [0x4670])
                self.assertTrue(all(int.from_bytes(data[:2], 'little') == 1
                                    for i, data in bus.sent if i == 0x4630))
                if not responses:
                    crc_time = next(f['monotonic'] for f in log.frames
                                    if f['id'] == 0x4670)
                    self.assertAlmostEqual(clock.now - crc_time, 300)

    def test_lost_block_ack_times_out_without_retry(self):
        updater, bus, log, clock = self.make_session()
        send = bus.send
        trailers = []

        def drop_first_ack(identifier, data):
            send(identifier, data)
            if identifier == 0x4670:
                trailers.append(clock.now)
                if len(trailers) == 1:
                    bus.queue.clear()

        bus.send = drop_first_ack
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(TimeoutError):
            updater.run(bytes(256))
        self.assertEqual([int.from_bytes(data[:2], 'little')
                          for i, data in bus.sent if i == 0x4630], [1])
        self.assertEqual(sum(i == 0x4670 for i, _ in bus.sent), 1)
        self.assertAlmostEqual(clock.now - trailers[0], 300)
        self.assertFalse(any('BLOCK_RETRY' in line for line in log.lines))

    def test_busy_bus_flushes_logs_without_aborting_block(self):
        updater, bus, log, clock = self.make_session()
        bus.responses[0x4670] = [ack(0x355, [50, 0], extended=False)] * 4200 + [
            ack(0x4681, [0xA2])]

        def recv(timeout):
            if bus.queue:
                clock.sleep(min(timeout, 0.001))
                return bus.queue.pop(0)
            clock.sleep(timeout)
            return None

        bus.recv = recv
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(updater.run(b'x'), 'status_query_sent')
        self.assertEqual(sum(f['id'] == 0x355 for f in log.frames), 4200)
        self.assertEqual(sum(i == 0x4670 for i, _ in bus.sent), 1)

    def test_block_success_near_total_deadline_allows_next_block(self):
        updater, bus, log, clock = self.make_session()
        send, recv = bus.send, bus.recv
        first_crc = []

        def send_with_delayed_ack(identifier, data):
            send(identifier, data)
            if identifier == 0x4670 and not first_crc:
                first_crc.append(clock.now)

        def delayed_recv(timeout):
            if bus.queue and bus.queue[0]['id'] == 0x4681 and clock.now < first_crc[0] + 55:
                delay = first_crc[0] + 55 - clock.now
                if delay >= timeout:
                    clock.sleep(timeout)
                    bus.queue.clear()
                    return None
                clock.sleep(delay)
                return bus.queue.pop(0)
            return recv(timeout)

        bus.send, bus.recv = send_with_delayed_ack, delayed_recv
        with contextlib.redirect_stdout(io.StringIO()):
            updater.run(bytes(256))
        headers = [f for f in log.frames if f['id'] == 0x4630]
        self.assertEqual([int.from_bytes(f['data'][:2], 'little') for f in headers],
                         [1, 2])
        self.assertAlmostEqual(headers[-1]['monotonic'] - first_crc[0], 55.047)

    def test_query_send_failure_is_not_success(self):
        updater, bus, _, _ = self.make_session()
        original_send = bus.send
        def send(identifier, data):
            if identifier == 0x46D0:
                raise OSError('query send failed')
            original_send(identifier, data)
        bus.send = send
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(OSError):
            updater.run(b'x')
        self.assertNotIn('level="100"', output.getvalue())
        self.assertNotIn('flow completed', output.getvalue())

    def test_size_limits_and_crc(self):
        self.assertEqual(u.crc16(b'123456789'), 0x4B37)
        u.validate_bslbatt_firmware(bytes(65535 * 128))
        for data in (b'', bytes(65535 * 128 + 1)):
            with self.assertRaises(u.FirmwareError):
                u.validate_bslbatt_firmware(data)

    def test_disabled_frame_logging_handles_busy_bus(self):
        for debug_enabled in (False, True):
            with self.subTest(debug=debug_enabled):
                updater, bus, _, clock = self.make_session()
                updater.log = u.Logger('', debug_enabled)
                bus.queue.append(ack(0x355, [50, 0], extended=False))
                bus.responses[0x4670] = [ack(0x355, [50, 0], extended=False)] * 4200 + [
                    ack(0x4681, [0xA2])]

                def recv(timeout):
                    clock.sleep(min(timeout, 0.001))
                    if bus.queue:
                        return bus.queue.pop(0)
                    clock.sleep(max(0, timeout - 0.001))
                    return None

                bus.recv = recv
                with patch.object(updater.log, 'frame_line') as format_frame, \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(updater.run(b'x'), 'status_query_sent')
                    format_frame.assert_not_called()
                self.assertIsNone(updater.events)


class IntegrationTests(unittest.TestCase):
    def test_list_receives_frames_with_and_without_debug(self):
        for debug_enabled in (False, True):
            args = u.build_parser().parse_args(
                ['--list', '-c', 'can0', '--timeout', '5'] +
                (['--debug'] if debug_enabled else []))
            clock = Clock()
            with self.subTest(debug=debug_enabled), patch.object(u, 'open_can') as open_can, \
                    patch.object(u.time, 'monotonic', clock), \
                    patch.object(u.select, 'select') as select, \
                    contextlib.redirect_stdout(io.StringIO()) as output, \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                sock = open_can.return_value
                sock.recv.return_value = u.FRAME.pack(u.CAN_ID_MANUFACTURER, 8, b'BSLBATT\x00')
                def readable(*args):
                    clock.sleep(1)
                    return [sock], [], []
                select.side_effect = readable
                self.assertEqual(u.list_devices(args), 0)
                device = ET.fromstring(output.getvalue())
                self.assertEqual(device.tag, 'device')
                self.assertEqual(device.get('connection'), 'socketcan:can0/0x0')
                self.assertEqual(device.get('type'), 'bslbatt')
                sock.close.assert_called_once()
                if debug_enabled:
                    self.assertIn('data=42 53 4C 42 41 54 54 00', errors.getvalue())
                else:
                    self.assertEqual(errors.getvalue(), '')

    def args(self, path):
        return u.build_parser().parse_args(['-c', 'can0', '-n', '0x0', '-f', str(path), '--can-log', ''])

    def test_single_attempt_exit_codes_and_cleanup(self):
        self.assertFalse(u.ENABLE_CAN_SERVICE_CONTROL)
        cases = [(None, 0), (TimeoutError('timeout'), 4), (OSError('CAN error'), 3),
                 (u.ProtocolError('CRC error', 8), 10), (u.ProtocolError('write error', 4), 8),
                 (u.ProtocolError('size error', 1), 5), (u.ProtocolError('sequence error', 19), 1),
                 (RuntimeError('unexpected'), 1), (KeyboardInterrupt(), 130)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Ptest.bin'
            path.write_bytes(b'abc')
            for error, code in cases:
                with self.subTest(code=code), patch.object(u, 'SocketCan') as factory, \
                        patch.object(u.os.path, 'isdir') as isdir, \
                        patch.object(u.subprocess, 'run') as svc, \
                        patch.object(u, 'stop_can_service', wraps=u.stop_can_service) as stop, \
                        patch.object(u, 'restore_can_service', wraps=u.restore_can_service) as restore, \
                        patch.object(u.BslbattFirmwareUpdater, 'run', side_effect=error) as run, \
                        contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(u.update(self.args(path)), code)
                    factory.assert_called_once_with('can0')
                    factory.return_value.__enter__.assert_called_once()
                    factory.return_value.__exit__.assert_called_once()
                    run.assert_called_once_with(b'abc')
                    isdir.assert_not_called()
                    svc.assert_not_called()
                    stop.assert_not_called()
                    restore.assert_not_called()
                    ET.fromstring('<output>' + output.getvalue() + '</output>')

    def test_can_init_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Ptest.bin'
            path.write_bytes(b'x')
            with patch.object(u, 'SocketCan') as factory, contextlib.redirect_stdout(io.StringIO()):
                factory.return_value.__enter__.side_effect = OSError('bind failed')
                self.assertEqual(u.update(self.args(path)), 2)
                factory.return_value.__exit__.assert_called_once()

    def test_disabled_service_helpers_do_not_touch_service(self):
        with patch.object(u, 'ENABLE_CAN_SERVICE_CONTROL', False), \
                patch.object(u, 'can_bms_service_path') as path, \
                patch.object(u.os.path, 'isdir') as isdir, \
                patch.object(u.subprocess, 'run') as svc:
            self.assertIsNone(u.stop_can_service('can0'))
            u.restore_can_service('/service/can-bus-bms.can0')
            path.assert_not_called()
            isdir.assert_not_called()
            svc.assert_not_called()

    def test_restore_runs_before_failing_diagnostic_output(self):
        service = '/service/can-bus-bms.can0'
        for command_error in (None, OSError(28, 'No space left on device')):
            with self.subTest(command_error=command_error), \
                    patch.object(u, 'ENABLE_CAN_SERVICE_CONTROL', True), \
                    patch.object(u.subprocess, 'run', side_effect=command_error) as svc:
                def failing_debug(*args):
                    svc.assert_called_once_with(['svc', '-u', service], check=False)
                    raise OSError(28, 'No space left on device')

                with patch.object(u, 'debug', side_effect=failing_debug):
                    u.restore_can_service(service, True)
                svc.assert_called_once_with(['svc', '-u', service], check=False)

    def test_signal_service_control_respects_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'firmware.bin'
            path.write_bytes(b'x')
            for enabled in (False, True):
                for signum in (u.signal.SIGINT, u.signal.SIGTERM):
                    handlers = {}

                    def register(sig, handler):
                        handlers[sig] = handler
                        return u.signal.SIG_DFL

                    def interrupt(_firmware):
                        handlers[signum](signum, None)

                    with self.subTest(enabled=enabled, signum=signum), \
                            patch.object(u, 'ENABLE_CAN_SERVICE_CONTROL', enabled), \
                            patch.object(u.signal, 'signal', side_effect=register), \
                            patch.object(u.os, 'kill', side_effect=SystemExit(128 + signum)) as kill, \
                            patch.object(u.os.path, 'isdir', return_value=True) as isdir, \
                            patch.object(u.subprocess, 'run') as svc, \
                            patch.object(u, 'restore_can_service', wraps=u.restore_can_service) as restore, \
                            patch.object(u, 'SocketCan') as factory, \
                            patch.object(u.BslbattFirmwareUpdater, 'run', side_effect=interrupt), \
                            contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            u.update(self.args(path))
                        kill.assert_called_once_with(u.os.getpid(), signum)
                        factory.return_value.__enter__.assert_called_once()
                        factory.return_value.__exit__.assert_called_once()
                        if enabled:
                            self.assertEqual(svc.call_args_list, [
                                call(['svc', '-d', '/service/can-bus-bms.can0'], check=True),
                                call(['svc', '-u', '/service/can-bus-bms.can0'], check=False),
                            ])
                        else:
                            isdir.assert_not_called()
                            svc.assert_not_called()
                            restore.assert_not_called()

    @patch.object(u, 'ENABLE_CAN_SERVICE_CONTROL', True)
    def test_update_stops_and_restores_selected_can_service_on_all_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Ptest.bin'
            path.write_bytes(b'x')
            service = '/service/can-bus-bms.can0'
            for error, exit_code in ((None, 0), (TimeoutError('timeout'), 4),
                                     (RuntimeError('unexpected'), 1), (KeyboardInterrupt(), 130)):
                with self.subTest(exit_code=exit_code), \
                        patch.object(u.os.path, 'isdir', return_value=True), \
                        patch.object(u.subprocess, 'run') as run_service, \
                        patch.object(u, 'SocketCan'), \
                        patch.object(u.BslbattFirmwareUpdater, 'run', side_effect=error), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(u.update(self.args(path)), exit_code)
                self.assertEqual(run_service.call_args_list, [
                    call(['svc', '-d', service], check=True),
                    call(['svc', '-u', service], check=False),
                ])

    @patch.object(u, 'ENABLE_CAN_SERVICE_CONTROL', True)
    def test_service_stays_stopped_until_communication_and_can_cleanup_finish(self):
        for timeout_request in (None, 0x4690, 0x46B0):
            with self.subTest(timeout_request=timeout_request), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'Ptest.bin'
                path.write_bytes(b'x')
                service = '/service/can-bus-bms.can0'
                _, bus, _, clock = session()
                if timeout_request is not None:
                    bus.responses[timeout_request] = []
                original_run = u.BslbattFirmwareUpdater.run
                output = io.StringIO()
                events = []

                def run_update(updater, firmware):
                    updater.clock, updater.sleep = clock, clock.sleep
                    try:
                        return original_run(updater, firmware)
                    finally:
                        self.assertEqual(events, ['-d'])
                        events.append('communication_finished')

                def service_command(command, **kwargs):
                    if command[1] == '-u':
                        self.assertEqual(events, ['-d', 'communication_finished', 'can_closed'])
                        if timeout_request is None:
                            self.assertEqual(bus.sent[-1][0], 0x46D0)
                            self.assertIn('Update flow completed', output.getvalue())
                    events.append(command[1])
                    return SimpleNamespace(returncode=0)

                with patch.object(u.os.path, 'isdir', return_value=True), \
                        patch.object(u.subprocess, 'run', side_effect=service_command) as run_service, \
                        patch.object(u, 'SocketCan') as factory, \
                        patch.object(u.BslbattFirmwareUpdater, 'run', run_update), \
                        contextlib.redirect_stdout(output):
                    factory.return_value.send.side_effect = bus.send
                    factory.return_value.recv.side_effect = bus.recv
                    factory.return_value.__exit__.side_effect = lambda: events.append('can_closed')
                    self.assertEqual(u.update(self.args(path)), 0 if timeout_request is None else 4)
                self.assertEqual(run_service.call_args_list, [
                    call(['svc', '-d', service], check=True),
                    call(['svc', '-u', service], check=False),
                ])
                self.assertEqual(events, ['-d', 'communication_finished', 'can_closed', '-u'])

    @patch.object(u, 'ENABLE_CAN_SERVICE_CONTROL', True)
    def test_service_stop_failure_prevents_can_update(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Ptest.bin'
            path.write_bytes(b'x')
            with patch.object(u.os.path, 'isdir', return_value=True), \
                    patch.object(u.subprocess, 'run', side_effect=OSError('svc missing')), \
                    patch.object(u, 'SocketCan') as factory, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(u.update(self.args(path)), 2)
            factory.assert_not_called()
            self.assertIn('CAN init failed', output.getvalue())

    def test_protocol_errors_use_english_public_messages_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'firmware.bin'
            path.write_bytes(b'abc')
            for status in u.CODES:
                if status in (0xA1, 0xA2, 0xA3, 10, 11, 12, 13, 16):
                    continue
                expected = (10, 'Verification failed') if status in (2, 8) else (
                    (8, 'Device memory error') if status in (4, 6, 20) else (
                        (5, 'Firmware error') if status in (1, 5, 7, 9, 18) else
                        (1, 'Update failed')))
                detail = 'BMS 0x{:02X}: {}'.format(status, u.CODES[status])
                args = self.args(path)
                args.can_log = str(Path(directory) / 'can.log')
                with self.subTest(status=status), patch.object(u, 'SocketCan'), \
                        patch.object(u.BslbattFirmwareUpdater, 'run',
                                     side_effect=u.ProtocolError(detail, status)), \
                        contextlib.redirect_stdout(io.StringIO()) as output, \
                        contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(u.update(args), expected[0])
                    message = ET.fromstring(output.getvalue())
                    self.assertEqual(message.attrib, {'type': 'normal'})
                    self.assertEqual(message.text, expected[1])
                    self.assertIn(detail, errors.getvalue())
                    self.assertIn(detail, Path(args.can_log).read_text())
                    for text in (output.getvalue(), errors.getvalue(), Path(args.can_log).read_text()):
                        self.assertTrue(text.isascii(), text)

    def test_can_status_descriptions_are_english(self):
        for identifier, statuses in u.VALID_CODES.items():
            for status in statuses | {0xFF}:
                payload = bytes([status])
                if (identifier, status) in ((0x4621, 0xA1), (0x46A1, 0xA3)):
                    payload += b'\x00\x80'
                self.assertTrue(u.decode(identifier, payload).isascii())

    def test_arbitrary_filenames_and_zip_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('Ptest.bin', 'BSLtest.bin', 'ptest.bin'):
                raw = Path(directory) / name
                raw.write_bytes(b'abc')
                archive = Path(directory) / 'upload.zip'
                with zipfile.ZipFile(archive, 'w') as stream:
                    stream.writestr('firmware/' + name, b'abc')
                for path in (raw, archive):
                    self.assertEqual(u.read_firmware(str(path)), b'abc')
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.writestr('Pone.bin', b'a')
                stream.writestr('Ptwo.bin', b'b')
            with self.assertRaises(u.FirmwareError):
                u.read_firmware(str(archive))

    def test_vrm_hash_filename_reaches_updater(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'aace1cf66d80316eca7bb14c87182581_aace1cf66d80316eca7bb14c87182581'
            path.write_bytes(b'firmware')
            args = u.build_parser().parse_args([
                '-u', '-c', 'socketcan:can0/0x0', '-f', str(path), '--can-log', ''])
            with patch.object(u, 'SocketCan') as factory, \
                    patch.object(u.BslbattFirmwareUpdater, 'run') as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(u.update(args), 0)
                factory.assert_called_once_with('can0')
                run.assert_called_once_with(b'firmware')

    def test_legacy_connection_and_inferred_modes(self):
        args = u.build_parser().parse_args(['--connection', 'socketcan:can0/0x0', '-f', '/tmp/P.bin'])
        self.assertEqual(u.resolve_update_target(args), ('can0', 0))
        self.assertEqual(u.infer_mode(args), 'update')
        self.assertEqual(u.infer_mode(u.build_parser().parse_args(['-c', 'can0'])), 'list')
        args.node_id = '0x2'
        with self.assertRaises(ValueError):
            u.resolve_update_target(args)

    def test_logging_is_xml_safe_and_failure_is_nonfatal(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()), \
                tempfile.TemporaryDirectory() as directory:
            log = u.Logger(str(Path(directory) / 'can.log'), True)
            log.write('test <&>')
            log.frame(ack(0x4681, [0xA2]))
            log.close()
            self.assertIn('BLOCK_ACK', (Path(directory) / 'can.log').read_text())
            log = u.Logger(str(Path(directory) / 'missing' / 'can.log'), True)
            log.write('still running')
            log.close()
            self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
