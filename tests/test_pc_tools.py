"""Offline protocol tests: no CAN hardware or external packages required."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import pc_update as u
import pc_can_monitor as monitor


def config(**overrides):
    args = u.parser().parse_args([])
    for key, value in dict(field_endian='big', crc_endian='little', tail_padding='ff',
                           tail_size='actual', block_crc_scope='padded',
                           firmware_crc_scope='actual', block_crc_order='data_number',
                           block_crc_endian='little', block_control_padding=False,
                           frame_interval=0, block_data_id_increment=False, block_size_field='actual',
                           firmware_size_padding=False, final_control_padding=False, restart_settle_delay=0,
                           completion_criterion='device_status',
                           block_crc_number_padding=False, block_size_includes_number=False).items():
        setattr(args, key, value)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def ack(identifier, payload, **overrides):
    frame = dict(id=identifier, data=bytes(payload), extended=True, remote=False,
                 error=False, direction='RX')
    frame.update(overrides)
    return frame


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Log:
    frames_enabled = True

    def __init__(self):
        self.lines, self.frames = [], []

    def write(self, line):
        self.lines.append(line)

    def frame(self, frame, interface=''):
        self.frames.append(frame)

    def batch_frames(self, frames, interface=''):
        self.frames.extend(frames)

    def detail(self, line):
        self.lines.append(line)


class Bus:
    def __init__(self, clock, responses=None):
        self.clock, self.sent, self.queue = clock, [], []
        self.responses = responses or {}

    def send(self, identifier, data):
        self.sent.append((identifier, data))
        self.queue.extend(self.responses.get(identifier, []))

    def recv(self, timeout):
        self.clock.sleep(min(timeout, 0.01))
        if self.queue:
            return self.queue.pop(0)
        self.clock.sleep(max(0, timeout - 0.01))
        return None


def session(args=None, responses=None):
    clock, log = Clock(), Log()
    if responses is None:
        responses = {0x4610: [ack(0x4621, [0xA1, 0, 128])],
                     0x4670: [ack(0x4681, [0xA2])],
                     0x4690: [ack(0x46A1, [0xA3, 0, 1])],
                     0x46B0: [ack(0x46C1, [11])],
                     0x46D0: [ack(0x46E1, [13])]}
    bus = Bus(clock, responses)
    return u.Updater(bus, args or config(), log, clock, clock.sleep), bus, log, clock


class EncodingTests(unittest.TestCase):
    def test_modbus_known_vector(self):
        self.assertEqual(u.crc16(b'123456789'), 0x4B37)

    def test_observed_first_block_and_current_experiment(self):
        data = bytes.fromhex('''
            B0 A0 00 20 ED 8F 04 08 25 FB 03 08 27 FB 03 08
            29 FB 03 08 2B FB 03 08 2D FB 03 08 00 00 00 00
            00 00 00 00 00 00 00 00 00 00 00 00 2F FB 03 08
            31 FB 03 08 00 00 00 00 0D 90 04 08 33 FB 03 08
            11 90 04 08 15 90 04 08 19 90 04 08 25 05 04 08
            1D 90 04 08 21 90 04 08 FD FF 03 08 77 03 04 08
            F9 03 04 08 25 90 04 08 29 90 04 08 2D 90 04 08
            31 90 04 08 35 90 04 08 39 90 04 08 3D 90 04 08
        ''')
        self.assertEqual(len(data), 128)
        self.assertEqual(u.crc16(data + b'\x01\x00'), 0x2980)
        self.assertEqual(u.crc16(b'\x01\x00' + data), 0x43FE)
        old = next(u.blocks(data, config(field_endian='big')))[2]
        last = next(u.blocks(data, config(field_endian='little')))[2]
        previous = next(u.blocks(data, config(field_endian='little',
                                              block_crc_order='number_data',
                                              block_crc_endian='big')))[2]
        cfg = u.parser().parse_args([])
        new = next(u.blocks(data, cfg))[2]
        self.assertTrue(cfg.block_control_padding)
        self.assertEqual(new[0], (0x4630, bytes.fromhex('01 00')))
        self.assertIn('crc_input_len=128', u.block_diagnostic(1, 128, new, cfg))
        self.assertEqual(old[-1], (0x4670, bytes.fromhex('80 29 00 80')))
        self.assertEqual(last[-1], (0x4670, bytes.fromhex('80 29 80 00')))
        self.assertEqual(previous[-1], (0x4670, bytes.fromhex('43 fe 80 00')))
        self.assertEqual(new[-1], (0x4670, bytes.fromhex('00 a2 00 00')))
        self.assertEqual(previous[:-1], new[:-1])
        self.assertEqual(u.block_crc_input(b'\x01\x00', data, cfg), data)
        # Negotiation remains big endian; firmware CRC does not change.
        self.assertEqual(cfg.response_size_endian, 'big')
        self.assertEqual(u.firmware_crc(data, cfg), u.firmware_crc(data, config()))

    def test_crc_includes_wire_number_padding_experiment(self):
        cfg = u.parser().parse_args([])
        cfg.block_control_padding = True
        cfg.block_crc_number_padding = True
        cfg.block_crc_order = 'number_data'
        data = bytes(range(128))
        frames = next(u.blocks(data, cfg))[2]
        wire_number = b'\x01' + b'\x00' * 7
        self.assertEqual(u.block_crc_input(frames[0][1], data, cfg), wire_number + data)
        self.assertEqual(frames[-1][1][:2], u.crc16(wire_number + data).to_bytes(2, 'little'))
        self.assertEqual([identifier for identifier, _ in frames[1:-1]], [0x4650] * 16)
        self.assertIn('crc_input_len=136', u.block_diagnostic(1, 128, frames, cfg))
        cfg.block_control_padding = False
        with self.assertRaises(ValueError):
            next(u.blocks(data, cfg))

    def test_boundary_lengths_and_reassembly(self):
        for size in (1, 127, 128, 129, 106020):
            data = bytes(i % 256 for i in range(size))
            for padding in ('none', '00', 'ff'):
                with self.subTest(size=size, padding=padding):
                    cfg = config(tail_padding=padding)
                    records = list(u.blocks(data, cfg))
                    self.assertEqual(len(records), (size + 127) // 128)
                    rebuilt = b''
                    for number, actual, frames in records:
                        self.assertEqual(int.from_bytes(frames[0][1], cfg.block_number_endian), number)
                        transmitted = b''.join(payload for identifier, payload in frames if identifier == 0x4650)
                        self.assertTrue(all(len(payload) <= 8 for _, payload in frames))
                        self.assertEqual(len(transmitted), actual if padding == 'none' else 128)
                        self.assertEqual(int.from_bytes(frames[-1][1][2:], 'big'), actual)
                        rebuilt += transmitted[:actual]
                    self.assertEqual(rebuilt, data)

    def test_independent_endianness_and_crc_scopes(self):
        for field in ('big', 'little'):
            for crc_order in ('big', 'little'):
                for scope in ('actual', 'padded'):
                    cfg = config(field_endian=field, block_number_endian=field, block_crc_endian=crc_order, crc_endian=crc_order,
                                 block_crc_scope=scope, firmware_crc_scope=scope,
                                 tail_size='padded')
                    _, _, frames = next(u.blocks(b'\x12', cfg))
                    payload = b'\x12' + (b'\xff' * 127 if scope == 'padded' else b'')
                    number = (1).to_bytes(2, field)
                    self.assertEqual(frames[-1][1], u.crc16(payload + number).to_bytes(2, crc_order) + (128).to_bytes(2, field))
                    self.assertEqual(u.firmware_crc(b'\x12', cfg), u.crc16(payload).to_bytes(2, crc_order))

    def test_little_block_number_keeps_size_big_and_updates_crc(self):
        data = bytes(range(128))
        frames = next(u.blocks(data, config()))[2]
        self.assertEqual(frames[0], (0x4630, b'\x01\x00'))
        self.assertEqual(frames[-1], (0x4670,
            u.crc16(data + b'\x01\x00').to_bytes(2, 'little') + b'\x00\x80'))
        # Exercise the byte boundary, not just the first block.
        records = list(u.blocks(data * 256, config()))
        self.assertEqual(records[255][2][0], (0x4630, b'\x00\x01'))

    def test_block_crc_byte_swap_is_isolated(self):
        data = bytes(range(128))
        old_config = config(block_crc_endian='big')
        new_config = config()
        old_frames = next(u.blocks(data, old_config))[2]
        new_frames = next(u.blocks(data, new_config))[2]
        self.assertEqual(new_frames[:-1], old_frames[:-1])
        old_trailer, new_trailer = old_frames[-1][1], new_frames[-1][1]
        self.assertEqual(new_trailer[:2], old_trailer[:2][::-1])
        self.assertEqual(new_trailer[2:], old_trailer[2:])
        self.assertEqual(u.firmware_crc(data, new_config), u.firmware_crc(data, old_config))

    def test_diagnostics_match_transmitted_crc(self):
        cfg = config()
        number, actual, frames = next(u.blocks(bytes(range(128)), cfg))
        diagnostic = u.block_diagnostic(number, actual, frames, cfg)
        self.assertIn('crc_input_len=130', diagnostic)
        self.assertIn('number=[01 00]', diagnostic)
        self.assertIn('crc16=0x{:04X}'.format(int.from_bytes(frames[-1][1][:2], cfg.block_crc_endian)), diagnostic)
        self.assertIn('trailer=[{}]'.format(frames[-1][1].hex(' ')), diagnostic)

    def test_crc_order_only_changes_crc_and_diagnostic_input(self):
        data = bytes(range(128))
        old = next(u.blocks(data, config(block_crc_order='number_data')))[2]
        new = next(u.blocks(data, config()))[2]
        self.assertEqual(old[:-1], new[:-1])
        self.assertEqual(old[-1][1][2:], new[-1][1][2:])
        self.assertEqual(u.block_crc_input(b'\x01\x00', data, config()), data + b'\x01\x00')
        self.assertEqual(u.block_crc_input(b'\x01\x00', data, config(block_crc_order='number_data')), b'\x01\x00' + data)

    def test_wire_frame_and_decoder(self):
        raw = u.FRAME.pack(0x4621 | u.EFF, 3, b'\xa1\x00\x80'.ljust(8, b'\x00'))
        self.assertEqual(u.unpack_frame(raw)['data'], b'\xa1\x00\x80')
        self.assertIn('大小通过', u.decode(0x4621, b'\xa1\x00\x80'))
        self.assertIn('invalid_length', u.decode(0x4621, b'\xa1'))
        self.assertIn('invalid_length', u.decode(0x4670, b'\x00'))
        self.assertIn('CRC 不匹配', u.decode(0x4681, b'\x02'))
        with self.assertRaises(ValueError):
            u.unpack_frame(u.FRAME.pack(1, 9, b'\x00' * 8))


class FlowTests(unittest.TestCase):
    def test_block_control_padding_only_changes_wire_dlc(self):
        cfg = config(block_control_padding=True)
        updater, bus, log, _ = session(cfg)
        data = bytes(range(128))
        updater.run(data)
        expected = next(u.blocks(data, cfg))[2]
        for identifier, payload in expected:
            wire = payload.ljust(8, b'\x00') if identifier in (0x4630, 0x4670) else payload
            self.assertIn((identifier, wire), bus.sent)
        self.assertEqual(bus.sent[0], (0x4610, b'\x80\x00\x00\x00'))
        self.assertIn('zero_padding=6', u.decode(0x4630, b'\x01' + b'\x00' * 7))

    def test_complete_only_after_final_success(self):
        updater, bus, log, _ = session()
        updater.run(b'x' * 129)
        ids = [identifier for identifier, _ in bus.sent]
        self.assertEqual(ids.count(0x4650), 32)
        self.assertEqual(ids.count(0x4670), 2)
        self.assertEqual(ids[-3:], [0x4690, 0x46B0, 0x46D0])
        self.assertEqual(log.lines[-1], '100% Update successful')

    def test_stop_stages(self):
        for stage, last in (('size', 0x4610), ('transfer', 0x4670), ('verify', 0x4690)):
            updater, bus, log, _ = session(config(stop_after=stage))
            updater.run(b'x')
            self.assertEqual(bus.sent[-1][0], last)
            self.assertIn('尚未完成升级', log.lines[-1])
            self.assertFalse(any('successful' in line for line in log.lines))

    def test_bad_frames_do_not_extend_deadline(self):
        updater, bus, log, clock = session(config(ack_timeout=0.025))
        bus.queue = [ack(0x4682, [0xA2]), ack(0x4681, [0xA2], extended=False),
                     ack(0x4681, [])] * 5
        with self.assertRaises(TimeoutError):
            updater.wait_ack(0x4681, {0xA2})
        self.assertAlmostEqual(clock.now, 0.025)

    def test_all_defined_error_responses(self):
        successes = {0x4621: {0xA1}, 0x4681: {0xA2}, 0x46A1: {0xA3},
                     0x46C1: {10, 11}, 0x46E1: {12, 13, 16}}
        for identifier, codes in u.VALID_CODES.items():
            for code in codes - successes[identifier]:
                updater, bus, _, _ = session()
                bus.queue = [ack(identifier, [code])]
                with self.subTest(identifier=identifier, code=code), self.assertRaises(u.ProtocolError):
                    updater.wait_ack(identifier, successes[identifier])

    def test_observed_padded_size_error_fails_immediately(self):
        updater, bus, log, clock = session(responses={
            0x4610: [ack(0x4621, [1, 0, 0, 0, 0, 0, 0, 0])]})
        with self.assertRaisesRegex(u.ProtocolError, '固件大小错误'):
            updater.run(b'x')
        self.assertEqual([identifier for identifier, _ in bus.sent], [0x4610])
        self.assertLess(clock.now, updater.config.ack_timeout)
        self.assertIn('固件大小错误', u.decode(0x4621, bytes([1] + [0] * 7)))

    def test_observed_little_size_and_big_negotiation(self):
        updater, bus, log, _ = session(config(stop_after='size'), responses={
            0x4610: [ack(0x4621, [0xA1, 0, 128, 0, 0, 0, 0, 0])]})
        updater.run(b'x' * 278820)
        self.assertEqual(bus.sent, [(0x4610, bytes.fromhex('24 41 04 00'))])
        self.assertIn('阶段完成', log.lines[-1])
        self.assertEqual(next(u.blocks(b'x', config()))[2][0][1], b'\x01\x00')
        preview_log = Log()
        u.preview(b'x' * 278820, config(stop_after='size'), preview_log)
        self.assertIn('24 41 04 00', preview_log.lines[0])

    def test_padded_success_flow(self):
        updater, bus, log, _ = session()
        for frames in bus.responses.values():
            for frame in frames:
                frame['data'] = frame['data'].ljust(8, b'\x00')
        updater.run(b'x')
        self.assertEqual(log.lines[-1], '100% Update successful')

    def test_nonzero_padding_and_partial_payload_rejected(self):
        for data in (b'\xa1', b'\xa1\x00', b'\xa1\x00\x80\x00',
                     b'\xa1\x00\x80\x00\x00\x00\x00\x01'):
            with self.assertRaises(ValueError):
                u.response_payload(0x4621, data)

    def test_unknown_status_rejected(self):
        updater, bus, _, _ = session()
        bus.queue = [ack(0x4681, [0xFF])]
        with self.assertRaises(u.ProtocolError):
            updater.wait_ack(0x4681, {0xA2})

    def test_unsupported_block_size(self):
        updater, _, _, _ = session(responses={0x4610: [ack(0x4621, [0xA1, 1, 0])]})
        with self.assertRaises(u.ProtocolError):
            updater.run(b'x')

    def test_data_timeout_has_no_retry(self):
        updater, bus, log, _ = session(responses={0x4610: [ack(0x4621, [0xA1, 0, 128])]})
        with self.assertRaises(TimeoutError):
            updater.run(b'x')
        self.assertEqual(sum(identifier == 0x4670 for identifier, _ in bus.sent), 1)
        self.assertFalse(any('successful' in line for line in log.lines))

    def test_status_silence_then_busy_then_success(self):
        updater, bus, log, _ = session(config(ack_timeout=1, update_timeout=10))
        original = bus.send
        results = [[], [ack(0x46E1, [12])], [ack(0x46E1, [16])], [ack(0x46E1, [13])]]
        def send(identifier, data):
            bus.responses[identifier] = results.pop(0)
            original(identifier, data)
        bus.send = send
        updater.poll_status()
        self.assertEqual(len(bus.sent), 4)
        self.assertIn('100%', log.lines[-1])

    def test_status_deadline(self):
        updater, bus, log, clock = session(config(ack_timeout=2, update_timeout=3),
                                         responses={0x46D0: [ack(0x46E1, [16])]})
        with self.assertRaises(TimeoutError):
            updater.poll_status()
        self.assertAlmostEqual(clock.now, 3)
        self.assertFalse(any('successful' in line for line in log.lines))


class CliTests(unittest.TestCase):
    def test_no_arguments_execute_fixed_firmware(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'P41288V110-41289-1.52T-000.bin'
            path.write_bytes(b'abc')
            overrides = dict(file=str(path), log=str(Path(directory) / 'update.log'))
            with patch.dict(u.FIXED_CONFIG, overrides), patch.object(u, 'SocketCan') as factory, \
                    patch.object(u, 'Updater') as updater, patch.object(u, 'check_device_online') as online, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(u.main([]), 0)
                factory.assert_called_once_with('can0')
                updater.return_value.run.assert_called_once_with(b'abc')
                self.assertEqual([call.args[2] for call in online.call_args_list], ['before', 'after'])
                args = updater.call_args.args[1]
                self.assertEqual(args.stop_after, 'complete')
                self.assertEqual(args.field_endian, 'little')
                self.assertEqual(args.crc_endian, 'little')
                self.assertEqual(args.tail_padding, 'ff')
                self.assertEqual(args.tail_size, 'actual')
                self.assertEqual(args.block_crc_scope, 'padded')
                self.assertEqual(args.firmware_crc_scope, 'actual')
                factory.return_value.__exit__.assert_called_once()
            self.assertIn('CONFIG', Path(overrides['log']).read_text())

    def test_fixed_firmware_path_is_script_relative(self):
        self.assertEqual(Path(u.FIXED_CONFIG['file']), Path(u.__file__).resolve().parent /
                         'P41288V110-41289-1.52T-000.bin')

    def test_command_line_overrides_rejected(self):
        with patch.object(u, 'SocketCan') as factory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                u.main(['--field-endian', 'little'])
            self.assertEqual(raised.exception.code, 2)
            factory.assert_not_called()

    def test_invalid_firmware_never_opens_can(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.bin'
            overrides = dict(file=str(path), log=str(Path(directory) / 'update.log'))
            for size in (None, 0, 65535 * 128 + 1):
                if size is not None:
                    with path.open('wb') as stream:
                        stream.truncate(size)
                with patch.dict(u.FIXED_CONFIG, overrides), patch.object(u, 'SocketCan') as factory, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(u.main([]), 1)
                    factory.assert_not_called()

    def test_monitor_count_filter_and_cleanup(self):
        with patch.object(monitor, 'SocketCan') as factory, contextlib.redirect_stdout(io.StringIO()) as output:
            bus = factory.return_value.__enter__.return_value
            bus.recv.side_effect = [ack(0x351, [1], extended=False), ack(0x46E1, [13])]
            self.assertEqual(monitor.main(['--count', '1', '--upgrade-only']), 0)
            self.assertIn('升级成功', output.getvalue())
            factory.return_value.__exit__.assert_called_once()

    def test_monitor_interrupt_cleanup(self):
        with patch.object(monitor, 'SocketCan') as factory:
            factory.return_value.__enter__.return_value.recv.side_effect = KeyboardInterrupt
            self.assertEqual(monitor.main([]), 0)
            factory.return_value.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
