#!/usr/bin/env python3
"""Passive Linux SocketCAN monitor with experimental BSLBATT upgrade decoding."""
import argparse
import math
import sys
import time
from pc_update import Logger, SocketCan, REQUESTS, RESPONSES


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('-c', '--can', default='can0')
    p.add_argument('--timeout', type=float)
    p.add_argument('--count', type=int, default=0, help='displayed frame limit; 0 means unlimited')
    p.add_argument('--log')
    p.add_argument('--upgrade-only', action='store_true')
    args = p.parse_args(argv)
    if args.count < 0 or (args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0)):
        p.error('count must be nonnegative and timeout finite and positive')
    log = None
    try:
        log = Logger(args.log)
        deadline = time.monotonic() + args.timeout if args.timeout is not None else float('inf')
        displayed = 0
        with SocketCan(args.can) as bus:
            while not args.count or displayed < args.count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                frame = bus.recv(min(1, remaining))
                if frame is None:
                    continue
                if args.upgrade_only and (not frame['extended'] or frame['id'] not in REQUESTS and frame['id'] not in RESPONSES):
                    continue
                log.frame(frame, args.can)
                displayed += 1
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print('Monitor failed: {}'.format(exc), file=sys.stderr)
        return 1
    finally:
        if log:
            log.close()


if __name__ == '__main__':
    sys.exit(main())
