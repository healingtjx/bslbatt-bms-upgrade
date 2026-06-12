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

SOL_CAN_RAW = 101
CAN_RAW_RECV_OWN_MSGS = 4

DIRECTION_TX = "TX"
DIRECTION_RX = "RX"

# BSLBATT BMS 升级协议关键 CAN ID（与 venus_firmware_update.py 保持一致）。
BMS_UPGRADE_START_REQ_ID = 0x18A055AA
BMS_UPGRADE_START_ACK_ID = 0x18A0AA55
BMS_UPGRADE_DATA_FRAME_BASE_ID = 0x13000001
BMS_UPGRADE_DATA_ACK_ID = 0x18A1AA55
BMS_UPGRADE_FINISH_REQ_ID = 0x18A255AA
BMS_UPGRADE_FINISH_ACK_ID = 0x18A2AA55
BMS_UPGRADE_ERROR_ID = 0x18A3AA55

# 数据帧 ID 规则：基址 0x13000001，每发一帧 +1，高 8 位固定 0x13。
BMS_UPGRADE_DATA_FRAME_ID_MASK = 0xFF000000
BMS_UPGRADE_DATA_FRAME_ID_PREFIX = 0x13000000

BMS_UPGRADE_CONTROL_NAMES = {
    BMS_UPGRADE_START_REQ_ID: "UPGRADE_START_REQ",
    BMS_UPGRADE_START_ACK_ID: "UPGRADE_START_ACK",
    BMS_UPGRADE_DATA_ACK_ID: "UPGRADE_DATA_ACK",
    BMS_UPGRADE_FINISH_REQ_ID: "UPGRADE_FINISH_REQ",
    BMS_UPGRADE_FINISH_ACK_ID: "UPGRADE_FINISH_ACK",
    BMS_UPGRADE_ERROR_ID: "UPGRADE_ERROR",
}


def is_upgrade_data_frame(can_id):
    """高 8 位 = 0x13 且不低于基址，视为升级数据帧。"""
    return (
        (can_id & BMS_UPGRADE_DATA_FRAME_ID_MASK) == BMS_UPGRADE_DATA_FRAME_ID_PREFIX
        and can_id >= BMS_UPGRADE_DATA_FRAME_BASE_ID
    )


def upgrade_frame_tag(can_id):
    """返回升级帧标签；非升级帧返回 None。

    数据帧附带序号（基址即第 0 帧，往后 +1），便于在抓帧日志里直接定位。
    """
    if can_id in BMS_UPGRADE_CONTROL_NAMES:
        return BMS_UPGRADE_CONTROL_NAMES[can_id]
    if is_upgrade_data_frame(can_id):
        frame_index = can_id - BMS_UPGRADE_DATA_FRAME_BASE_ID
        return "UPGRADE_DATA#{}".format(frame_index)
    return None


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
        try:
            self.sock.setsockopt(SOL_CAN_RAW, CAN_RAW_RECV_OWN_MSGS, 1)
        except OSError:
            pass
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
                raw_frame, _ancdata, msg_flags, _addr = self.sock.recvmsg(CAN_FRAME_SIZE, 0)
            except OSError as exc:
                raise OSError("CAN receive failed: {}".format(exc))

            frame = unpack_can_frame(raw_frame)
            frame["direction"] = DIRECTION_TX if msg_flags & socket.MSG_DONTROUTE else DIRECTION_RX
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
    upgrade_tag = upgrade_frame_tag(frame["can_id"])
    tag_text = " tag={}".format(upgrade_tag) if upgrade_tag else ""
    print(
        "{timestamp:.3f} {interface} {direction} {frame_type} id=0x{can_id:08X} "
        "dlc={dlc} flags={flags} data={data}{tag}".format(
            timestamp=time.time(),
            interface=interface_name,
            direction=frame.get("direction", DIRECTION_RX),
            frame_type=frame_type,
            can_id=frame["can_id"],
            dlc=len(frame["data"]),
            flags=flag_text,
            data=format_payload(frame["data"]),
            tag=tag_text,
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
