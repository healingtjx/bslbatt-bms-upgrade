#!/usr/bin/env python3
"""
SocketCAN frame monitor for Victron GX / Venus OS field debugging.

Example:
    python3 venus_can_monitor.py
    python3 venus_can_monitor.py -c can0 --timeout 30
    python3 venus_can_monitor.py -c vecan0 --count 20
"""

import argparse
import select
import socket
import struct
import sys
import time


EXIT_OK = 0
EXIT_CAN_INIT_ERROR = 2
EXIT_CAN_COMM_ERROR = 3
EXIT_ARGUMENT_ERROR = 6

CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_ID_MASK = 0x1FFFFFFF


def configure_output():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True, write_through=True)


def format_payload(payload):
    return " ".join("{:02X}".format(byte) for byte in payload)


def unpack_can_frame(raw_frame):
    can_id, data_len, data = struct.unpack(CAN_FRAME_FORMAT, raw_frame)
    return {
        "can_id": can_id & CAN_ID_MASK,
        "is_extended": bool(can_id & CAN_EFF_FLAG),
        "is_remote": bool(can_id & CAN_RTR_FLAG),
        "is_error": bool(can_id & CAN_ERR_FLAG),
        "data": data[:data_len],
    }


class SocketCanMonitor:
    def __init__(self, interface_name):
        self.interface_name = interface_name
        self.sock = None

    def open(self):
        if not hasattr(socket, "AF_CAN") or not hasattr(socket, "CAN_RAW"):
            raise OSError("SocketCAN is required on Victron GX / Venus OS")
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.sock.bind((self.interface_name,))
        self.sock.setblocking(False)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def listen(self, timeout=None, count=0, include_errors=False):
        if self.sock is None:
            self.open()

        started_at = time.monotonic()
        received = 0
        while True:
            if count > 0 and received >= count:
                return received

            if timeout is None:
                wait = 1.0
            else:
                remaining = timeout - (time.monotonic() - started_at)
                if remaining <= 0:
                    return received
                wait = min(1.0, remaining)

            try:
                readable, _, _ = select.select([self.sock], [], [], wait)
            except OSError as exc:
                raise OSError("CAN select failed: {}".format(exc))

            if not readable:
                continue

            try:
                raw_frame = self.sock.recv(CAN_FRAME_SIZE)
            except OSError as exc:
                raise OSError("CAN receive failed: {}".format(exc))

            frame = unpack_can_frame(raw_frame)
            if frame["is_error"] and not include_errors:
                continue

            received += 1
            yield frame


def print_frame(interface_name, frame):
    frame_type = "EFF" if frame["is_extended"] else "SFF"
    flags = []
    if frame["is_remote"]:
        flags.append("RTR")
    if frame["is_error"]:
        flags.append("ERR")
    flag_text = ",".join(flags) if flags else "-"
    print(
        "{timestamp:.3f} {interface} {frame_type} id=0x{can_id:08X} "
        "dlc={dlc} flags={flags} data={data}".format(
            timestamp=time.time(),
            interface=interface_name,
            frame_type=frame_type,
            can_id=frame["can_id"],
            dlc=len(frame["data"]),
            flags=flag_text,
            data=format_payload(frame["data"]),
        ),
        flush=True,
    )


def build_parser():
    parser = argparse.ArgumentParser(description="Listen for raw SocketCAN frames")
    parser.add_argument("-c", "--can", default="can0", help="CAN interface to listen on, default: can0")
    parser.add_argument("--timeout", type=float, default=0.0, help="listen time in seconds; 0 means forever")
    parser.add_argument("--count", type=int, default=0, help="stop after N received frames; 0 means unlimited")
    parser.add_argument("--include-errors", action="store_true", help="print CAN error frames too")
    return parser


def main():
    configure_output()
    args = build_parser().parse_args()
    if args.timeout < 0 or args.count < 0:
        print("timeout and count must be non-negative", file=sys.stderr, flush=True)
        return EXIT_ARGUMENT_ERROR

    timeout = None if args.timeout == 0 else args.timeout
    monitor = SocketCanMonitor(args.can)
    try:
        monitor.open()
        print("Listening on {}...".format(args.can), file=sys.stderr, flush=True)
        received = 0
        for frame in monitor.listen(timeout=timeout, count=args.count, include_errors=args.include_errors):
            received += 1
            print_frame(args.can, frame)
        print("Stopped, received {} frame(s)".format(received), file=sys.stderr, flush=True)
        return EXIT_OK
    except OSError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return EXIT_CAN_INIT_ERROR if monitor.sock is None else EXIT_CAN_COMM_ERROR
    finally:
        monitor.close()


if __name__ == "__main__":
    sys.exit(main() & 0xFF)
