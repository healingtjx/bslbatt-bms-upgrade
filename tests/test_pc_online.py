import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import pc_update as u


ONLINE = '<device type="bslbatt" connection="socketcan:can0/0x0" serial="test" version="1.0" />'


class OnlineTests(unittest.TestCase):
    def test_empty_discovery_does_not_block_update(self):
        for output in ('', '<device type="other" connection="socketcan:can0/0x0" />',
                       ONLINE.replace('can0', 'can1')):
            with patch.object(u.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertEqual(
                    u.check_device_online(u.parser().parse_args([]), u.Logger(), 'before'), [])

    def test_online_and_tool_errors_are_distinct(self):
        cfg = u.parser().parse_args([])
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.object(u.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, ONLINE, '')) as run:
                self.assertEqual(len(u.check_device_online(cfg, u.Logger(), 'before')), 1)
                self.assertIn('--list', run.call_args.args[0])
                self.assertNotIn('--update', run.call_args.args[0])
            for code, output in ((2, ''), (0, 'not XML <')):
                with patch.object(u.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, output, '')):
                    with self.assertRaises(u.ProtocolError) as exc:
                        u.check_device_online(cfg, u.Logger(), 'before')
                    self.assertNotIsInstance(exc.exception, u.DeviceOffline)

    def test_offline_blocks_can_and_check_only_needs_no_firmware(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'test.bin'
            file.write_bytes(b'abc')
            overrides = dict(file=str(file), log=str(Path(directory) / 'debug.log'))
            with patch.dict(u.FIXED_CONFIG, overrides), patch.object(u, 'SocketCan') as bus, \
                    patch.object(u, 'check_device_online', side_effect=u.DeviceOffline('offline')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(u.main([]), 1)
                bus.assert_not_called()
            file.unlink()
            with patch.dict(u.FIXED_CONFIG, overrides), patch.object(u, 'SocketCan') as bus, \
                    patch.object(u, 'check_device_online') as check, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(u.main(['--check-online']), 0)
                self.assertEqual(check.call_args.args[2], 'check-only')
                bus.assert_not_called()

    def test_failed_upgrade_still_checks_after(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'test.bin'
            file.write_bytes(b'abc')
            overrides = dict(file=str(file), log=str(Path(directory) / 'debug.log'))
            with patch.dict(u.FIXED_CONFIG, overrides), patch.object(u, 'SocketCan'), \
                    patch.object(u, 'Updater') as updater, patch.object(u, 'check_device_online') as check, \
                    patch.object(u.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                updater.return_value.run.side_effect = u.ProtocolError('CRC failure')
                self.assertEqual(u.main([]), 1)
                self.assertEqual([call.args[2] for call in check.call_args_list], ['before', 'after'])

    def test_post_check_does_not_replace_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'test.bin'
            file.write_bytes(b'abc')
            log = Path(directory) / 'debug.log'
            with patch.dict(u.FIXED_CONFIG, dict(file=str(file), log=str(log))), \
                    patch.object(u, 'SocketCan'), patch.object(u, 'Updater') as updater, \
                    patch.object(u, 'can_diagnostic'), \
                    patch.object(u, 'check_device_online', side_effect=[None, u.DeviceOffline('offline')]), \
                    patch.object(u.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                updater.return_value.run.side_effect = TimeoutError('block ACK timeout')
                self.assertEqual(u.main([]), 4)
            self.assertIn('POST_CHECK failed: DeviceOffline', log.read_text())
            self.assertIn('FAILED: block ACK timeout', log.read_text())

    def test_upgrade_retry_limit_delay_and_interrupt(self):
        cases = (
            ([u.ProtocolError('CRC failure'), 'status_query_sent'], 0, 2),
            ([TimeoutError('first timeout'), TimeoutError('second timeout')], 4, 2),
            (['status_query_sent'], 0, 1),
            ([KeyboardInterrupt()], 130, 1),
        )
        for results, exit_code, attempts in cases:
            with self.subTest(exit_code=exit_code, attempts=attempts), tempfile.TemporaryDirectory() as directory:
                file = Path(directory) / 'test.bin'
                file.write_bytes(b'abc')
                with patch.dict(u.FIXED_CONFIG, dict(file=str(file), log=str(Path(directory) / 'debug.log'))), \
                        patch.object(u, 'SocketCan') as bus, patch.object(u, 'Updater') as updater, \
                        patch.object(u, 'can_diagnostic'), patch.object(u, 'check_device_online'), \
                        patch.object(u.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
                    updater.return_value.run.side_effect = results
                    self.assertEqual(u.main([]), exit_code)
                    self.assertEqual(bus.call_count, attempts)
                    self.assertEqual(bus.return_value.__exit__.call_count, attempts)
                    self.assertEqual(updater.call_count, attempts)
                    self.assertEqual(updater.return_value.run.call_count, attempts)
                    self.assertTrue(all(call.args == (b'abc',)
                                        for call in updater.return_value.run.call_args_list))
                    if attempts == 2:
                        sleep.assert_called_once_with(2)
                    else:
                        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
