#!/usr/bin/env python3
"""Alternate two BMS firmwares; continue only after confirmed device success."""
import argparse
import contextlib
from datetime import datetime
import fcntl
import importlib.util
from pathlib import Path
import sys
import time
import traceback


FIRMWARES = ('P41288V110-41289-1.51T-000.bin',
             'P41288V110-41289-1.52T-000.bin')
DEFAULT_LOG_DIR = Path('/opt/victronenergy/mqtt-rpc/thirdparty/bslbatt/logs')


def confirmed_updater(module, status_timeout):
    class ConfirmedUpdater(module.BslbattFirmwareUpdater):
        def poll_status(self):
            deadline = self.clock() + status_timeout
            self.log.write('Waiting for confirmed device success (0x46E1/0D)')
            self.sleep(min(self.config.restart_settle_delay, status_timeout))
            while self.clock() < deadline:
                next_query = min(self.clock() + 5.0, deadline)
                self.send(0x46D0, b'')
                try:
                    response = self.wait_ack(0x46E1, {12, 13, 16}, deadline=next_query)
                    if response[0] == 13:
                        self.log.write('completion=device_success status=0x0D')
                        module.xml_progress(100)
                        module.xml_message('Device update successful')
                        return 'device_success'
                except TimeoutError:
                    self.log.write('No status ACK; retrying status query')
                self.sleep(max(0, next_query - self.clock()))
            raise TimeoutError('device success was not confirmed within {} seconds'.format(status_timeout))
    return ConfirmedUpdater


class Tee:
    def __init__(self, terminal, log):
        self.terminal, self.log = terminal, log

    def write(self, text):
        self.terminal.write(text)
        self.log.write(text)
        self.flush()
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.log.flush()


def run_cycles(module, args, directory):
    run_dir = args.log_dir / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    run_dir.mkdir(parents=True)
    round_number = 0
    first = args.start
    with (run_dir / 'console.log').open('w', encoding='utf-8') as console:
        with contextlib.redirect_stdout(Tee(sys.stdout, console)), \
                contextlib.redirect_stderr(Tee(sys.stderr, console)):
            print('Logs: {}'.format(run_dir), flush=True)
            print('Cycle settings: can={} start={} rounds={} interval={} status_timeout={}'.format(
                args.can, args.start, args.rounds, args.interval, args.status_timeout), flush=True)
            try:
                while args.rounds == 0 or round_number < args.rounds:
                    firmware = directory / FIRMWARES[(first + round_number) % 2]
                    round_number += 1
                    can_log = run_dir / 'round-{:04d}-{}.log'.format(round_number, firmware.stem)
                    print('{} ROUND {} START {}'.format(datetime.now().isoformat(), round_number,
                                                       firmware.name), flush=True)
                    print('ROUND {} CAN log: {}'.format(round_number, can_log), flush=True)
                    update_args = module.build_parser().parse_args([
                        '--update', '-c', args.can, '-n', '0x0', '-f', str(firmware),
                        '--can-log', str(can_log), '--debug'])
                    code = module.update(update_args)
                    if code != 0:
                        print('ROUND {} FAILED exit={}; stopping. Log: {}'.format(
                            round_number, code, can_log), flush=True)
                        return code
                    print('ROUND {} SUCCESS (device confirmed)'.format(round_number), flush=True)
                    if args.rounds and round_number >= args.rounds:
                        return 0
                    print('Waiting {} seconds before next round; Ctrl+C to stop.'.format(
                        args.interval), flush=True)
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print('Stopped by user.', flush=True)
                return 130
            except Exception as exc:
                print('CYCLE ERROR {}: {}'.format(type(exc).__name__, exc), flush=True)
                traceback.print_exc()
                raise
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-c', '--can', default='can0')
    parser.add_argument('--start', type=int, choices=(0, 1), default=0,
                        help='firmware for the first round: 0=1.51, 1=1.52 (default: 0)')
    parser.add_argument('--rounds', type=int, default=0, help='total upgrades; 0 means forever')
    parser.add_argument('--interval', type=float, default=120, help='seconds after confirmed success')
    parser.add_argument('--status-timeout', type=float, default=300,
                        help='maximum seconds to confirm completion after restart ACK')
    parser.add_argument('--log-dir', type=Path, default=DEFAULT_LOG_DIR,
                        help='log root (default: {})'.format(DEFAULT_LOG_DIR))
    args = parser.parse_args(argv)
    if args.rounds < 0 or not 0 <= args.interval < float('inf') or not 0 < args.status_timeout < float('inf'):
        parser.error('rounds/interval must be nonnegative and status-timeout must be positive and finite')
    directory = Path(__file__).resolve().parent
    for name in ('bslbatt-tool.py',) + FIRMWARES:
        if not (directory / name).is_file():
            parser.error('missing file: {}'.format(directory / name))
    # Hold one lock across both transfer and cooldown; no competing cycle scripts.
    with open('/tmp/bslbatt-cycle.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('another bslbatt-cycle.py is already running')
        spec = importlib.util.spec_from_file_location('bslbatt_tool', directory / 'bslbatt-tool.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Validate both images before starting the first upgrade.
        for name in FIRMWARES:
            module.validate_bslbatt_firmware(module.read_firmware(str(directory / name)))
        module.BslbattFirmwareUpdater = confirmed_updater(module, args.status_timeout)
        return run_cycles(module, args, directory)


if __name__ == '__main__':
    sys.exit(main())
