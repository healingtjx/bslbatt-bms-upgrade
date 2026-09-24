import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_bslbatt_update import u
import test_bslbatt_update as updater_tests
from test_pc_tools import ack

spec = importlib.util.spec_from_file_location(
    'cycle', Path(__file__).resolve().parents[1] / 'tools' / 'bslbatt-cycle.py')
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)


class CycleTests(unittest.TestCase):
    def test_alternation_and_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(log_dir=Path(directory), start=0, rounds=3,
                                   can='can0', interval=120, status_timeout=300)
            with patch.object(u, 'update', return_value=0) as update, \
                    patch.object(cycle.time, 'sleep') as sleep, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cycle.run_cycles(u, args, Path(directory)), 0)
            self.assertEqual([Path(call.args[0].file).name for call in update.call_args_list],
                             [cycle.FIRMWARES[0], cycle.FIRMWARES[1], cycle.FIRMWARES[0]])
            self.assertEqual([call.args for call in sleep.call_args_list], [(120,), (120,)])
            console_logs = list(Path(directory).glob('*/console.log'))
            self.assertEqual(console_logs, [])
            console_text = output.getvalue()
            self.assertIn('Cycle settings: can=can0 start=0 rounds=3', console_text)
            self.assertIn('ROUND 1 CAN log:', console_text)

    def test_failure_stops_without_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(log_dir=Path(directory), start=1, rounds=0,
                                   can='can0', interval=120, status_timeout=300)
            with patch.object(u, 'update', return_value=4) as update, \
                    patch.object(cycle.time, 'sleep') as sleep, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cycle.run_cycles(u, args, Path(directory)), 4)
            update.assert_called_once()
            sleep.assert_not_called()

    def test_final_status_is_required(self):
        for status, expected in ((13, None), (14, u.ProtocolError), (16, TimeoutError),
                                 (None, TimeoutError)):
            for suffix in ([], [0] * 7, [1], [1] + [0] * 6):
                with self.subTest(status=status, suffix=suffix):
                    original, bus, log, clock = updater_tests.ProtocolTests().make_session()
                    bus.responses[0x46D0] = [] if status is None else [ack(0x46E1, [status] + suffix)]
                    updater = cycle.confirmed_updater(u, 25)(bus, original.config, log, clock, clock.sleep)
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        if expected:
                            with self.assertRaises(expected):
                                updater.poll_status()
                            self.assertNotIn('Device update successful', output.getvalue())
                        else:
                            self.assertEqual(updater.poll_status(), 'device_success')
                            self.assertEqual(len(bus.sent), 1)
                            self.assertFalse(any('No status ACK' in line for line in log.lines))
                    if expected is TimeoutError:
                        self.assertAlmostEqual(clock.now, 25)


if __name__ == '__main__':
    unittest.main()
