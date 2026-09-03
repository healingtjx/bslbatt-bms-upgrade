#!/usr/bin/env python3
"""
BSLBATT Venus OS device discovery tool.

Called by Victron GX / Venus OS to list updatable devices on a selected CAN bus.
Stdout must contain XML device lines only. Debug output goes to stderr.

Example:
    ./venus_device_list.py --list -c can0
"""

import argparse
import html
import re
import select
import socket
import struct
import subprocess
import sys
import time


EXIT_OK = 0
EXIT_GENERAL_ERROR = 1
EXIT_CAN_INIT_ERROR = 2
EXIT_CAN_COMM_ERROR = 3
EXIT_ARGUMENT_ERROR = 6

MANUFACTURER_TYPE = "bslbatt"
PRODUCT_ID = "0xB021"
DEVICE_DESCRIPTION = "BSLBATT BMS"
DEFAULT_NODE_ID = 0

CAN_ID_LIMITS = 0x351
CAN_ID_SOC = 0x355
CAN_ID_MEASUREMENTS = 0x356
CAN_ID_STATUS_OBSERVED = 0x359
CAN_ID_ALARMS = 0x35A
CAN_ID_FLAGS_OBSERVED = 0x35C
CAN_ID_MANUFACTURER = 0x35E
CAN_ID_BATTERY_INFO = 0x35F
CAN_ID_NAME_PART_1 = 0x370
CAN_ID_NAME_PART_2 = 0x371
CAN_ID_DEVICE_MARKER = 0x375
CAN_ID_CAPACITY_OBSERVED = 0x379
CAN_ID_SERIAL_PART_1 = 0x380
CAN_ID_SERIAL_PART_2 = 0x381
CAN_ID_FAMILY = 0x382

CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_ID_MASK = 0x1FFFFFFF

BMS_CAN_CORE_IDS = (CAN_ID_LIMITS, CAN_ID_SOC, CAN_ID_MEASUREMENTS, CAN_ID_ALARMS)
BMS_CAN_OBSERVED_IDS = (CAN_ID_STATUS_OBSERVED, CAN_ID_FLAGS_OBSERVED, CAN_ID_CAPACITY_OBSERVED)


def configure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True, write_through=True)


def debug(enabled, message):
    if enabled:
        print(message, file=sys.stderr, flush=True)


def xml_attr(value):
    return html.escape(str(value), quote=True)


def open_can(interface_name):
    if not hasattr(socket, "AF_CAN") or not hasattr(socket, "CAN_RAW"):
        raise OSError("SocketCAN is required on Victron GX / Venus OS")
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((interface_name,))
    sock.setblocking(False)
    return sock


def unpack_can_frame(raw_frame):
    can_id, data_len, data = struct.unpack(CAN_FRAME_FORMAT, raw_frame)
    return {
        "can_id": can_id & CAN_ID_MASK,
        "is_extended": bool(can_id & CAN_EFF_FLAG),
        "is_remote": bool(can_id & CAN_RTR_FLAG),
        "is_error": bool(can_id & CAN_ERR_FLAG),
        "data": data[:data_len],
    }


def payload_hex(payload):
    return " ".join("{:02X}".format(byte) for byte in payload)


def read_le16(data, offset):
    if len(data) < offset + 2:
        return None
    return data[offset] | (data[offset + 1] << 8)


def decode_battery_info_version(payload):
    """0x35F 的 BYTE2/BYTE3 表示固件版本，例如 02 03 12 45 => 12.45。"""
    if len(payload) < 4:
        return ""
    return "{}.{}".format(payload[2], payload[3])


def decode_victron_ascii(payload):
    """
    Decode Victron BMS-CAN 7-bit ASCII fields.

    NUL bytes are padding. Other control bytes are ignored so a noisy optional
    field cannot leak unreadable characters into Venus OS XML output.
    """
    chars = []
    for byte in payload:
        if byte == 0:
            continue
        byte &= 0x7F
        if 32 <= byte <= 126:
            chars.append(chr(byte))
    return "".join(chars).strip()


def join_text_parts(*parts):
    return "".join(part for part in parts if part).strip()


def contains_bslbatt(value):
    return "BSLBATT" in value.upper()


class BmsCanLvDeviceState:
    """
    Collect Victron BMS-CAN LV frames for one CAN interface.

    The protocol uses fixed 11-bit CAN identifiers instead of per-device node
    addresses, so one scanned interface can only produce one updatable record.
    """

    def __init__(self, can_interface):
        self.can_interface = can_interface
        self.seen_battery_marker = False
        self.seen_device_marker = False
        self.seen_core_frame = False
        self.observed_frame_ids = set()
        self.seen_identity_frame = False
        self.seen_bslbatt_identity = False
        self.manufacturer = ""
        self.family = ""
        self.name_part_1 = ""
        self.name_part_2 = ""
        self.serial_part_1 = ""
        self.serial_part_2 = ""
        self.model = None
        self.firmware_version = ""
        self.online_capacity_ah = None

    def update(self, frame):
        if frame["is_extended"] or frame["is_remote"] or frame["is_error"]:
            return

        can_id = frame["can_id"]
        payload = frame["data"]
        if can_id == CAN_ID_DEVICE_MARKER:
            self.seen_device_marker = True

        if can_id in BMS_CAN_CORE_IDS:
            self.seen_core_frame = True
        if can_id in BMS_CAN_OBSERVED_IDS:
            self.observed_frame_ids.add(can_id)
        if can_id == CAN_ID_ALARMS:
            self.seen_battery_marker = True
        elif can_id == CAN_ID_MANUFACTURER:
            self.manufacturer = decode_victron_ascii(payload)
            self._record_identity_text(self.manufacturer)
        elif can_id == CAN_ID_BATTERY_INFO:
            self.model = read_le16(payload, 0)
            self.online_capacity_ah = read_le16(payload, 4)
            self.firmware_version = decode_battery_info_version(payload)
        elif can_id == CAN_ID_NAME_PART_1:
            self.name_part_1 = decode_victron_ascii(payload)
            self._record_identity_text(self.name_part_1)
        elif can_id == CAN_ID_NAME_PART_2:
            self.name_part_2 = decode_victron_ascii(payload)
            self._record_identity_text(self.name_part_2)
        elif can_id == CAN_ID_SERIAL_PART_1:
            self.serial_part_1 = decode_victron_ascii(payload)
        elif can_id == CAN_ID_SERIAL_PART_2:
            self.serial_part_2 = decode_victron_ascii(payload)
        elif can_id == CAN_ID_FAMILY:
            self.family = decode_victron_ascii(payload)
            self._record_identity_text(self.family)

    def _record_identity_text(self, value):
        if value:
            self.seen_identity_frame = True
            if contains_bslbatt(value):
                self.seen_bslbatt_identity = True

    def is_bslbatt_candidate(self):
        if self.seen_device_marker:
            return True
        if self.seen_bslbatt_identity:
            return True
        if not self.seen_core_frame and len(self.observed_frame_ids) < 2:
            return False
        return not self.seen_identity_frame

    def to_device(self):
        if not self.is_bslbatt_candidate():
            return None

        serial = join_text_parts(self.serial_part_1, self.serial_part_2)
        if not serial:
            serial = self._fallback_serial()

        return {
            "node_id": DEFAULT_NODE_ID,
            "serial": serial,
            "version": self.firmware_version or "unknown",
            "description": self._description(),
        }

    def _description(self):
        name = join_text_parts(self.name_part_1, self.name_part_2)
        if name:
            return name
        parts = []
        if self.manufacturer:
            parts.append(self.manufacturer)
        if self.family:
            parts.append(self.family)
        if self.model is not None:
            parts.append("model {}".format(self.model))
        return " ".join(parts) if parts else DEVICE_DESCRIPTION

    def _fallback_serial(self):
        parts = []
        if self.manufacturer:
            parts.append(self.manufacturer)
        if self.family:
            parts.append(self.family)
        if self.model is not None:
            parts.append("model{}".format(self.model))
        parts.append(self.can_interface)
        return "-".join(parts)


def list_available_can_interfaces():
    """
    Return available GX CAN interfaces matching Victron's can/vecan naming.

    This intentionally only inspects existing interfaces. It does not change
    bitrate, state, or any other CAN-bus setting.
    """
    command = "ip link show 2>/dev/null | grep -oE '(can|vecan)[0-9]' | sort -u"
    try:
        output = subprocess.check_output(command, shell=True, universal_newlines=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    return [name for name in output.splitlines() if re.match(r"^(can|vecan)[0-9]+$", name)]


def print_device(device, can_interface, product_id, manufacturer_type):
    node_id = int(device["node_id"])
    connection = "socketcan:{}/0x{:X}".format(can_interface, node_id)
    print(
        '<device serial="{serial}" version="{version}" description="{description}" '
        'id="{product_id}" type="{manufacturer_type}" connection="{connection}" '
        'updatable="True" />'.format(
            serial=xml_attr(device["serial"]),
            version=xml_attr(device["version"]),
            description=xml_attr(device.get("description", DEVICE_DESCRIPTION)),
            product_id=xml_attr(product_id),
            manufacturer_type=xml_attr(manufacturer_type),
            connection=xml_attr(connection),
        ),
        flush=True,
    )


def list_devices_on_interface(can_interface, args, found):
    try:
        sock = open_can(can_interface)
    except OSError as exc:
        debug(args.debug, "CAN init failed on {}: {}".format(can_interface, exc))
        return EXIT_CAN_INIT_ERROR

    deadline = time.monotonic() + args.timeout
    device_state = BmsCanLvDeviceState(can_interface)
    debug(args.debug, "Listening on {} for {:.1f}s".format(can_interface, args.timeout))

    try:
        while time.monotonic() < deadline:
            wait = min(0.2, max(0.0, deadline - time.monotonic()))
            try:
                readable, _, _ = select.select([sock], [], [], wait)
            except OSError as exc:
                debug(args.debug, "CAN select failed on {}: {}".format(can_interface, exc))
                return EXIT_CAN_COMM_ERROR

            if not readable:
                continue

            try:
                raw_frame = sock.recv(CAN_FRAME_SIZE)
            except OSError as exc:
                debug(args.debug, "CAN receive failed on {}: {}".format(can_interface, exc))
                return EXIT_CAN_COMM_ERROR

            frame = unpack_can_frame(raw_frame)
            debug(
                args.debug,
                "RX {} id=0x{:08X} ext={} rtr={} err={} len={} data={}".format(
                    can_interface,
                    frame["can_id"],
                    int(frame["is_extended"]),
                    int(frame["is_remote"]),
                    int(frame["is_error"]),
                    len(frame["data"]),
                    payload_hex(frame["data"]),
                ),
            )
            device_state.update(frame)
    finally:
        sock.close()

    device = device_state.to_device()
    if device is None:
        debug(
            args.debug,
            "No BSLBATT BMS-CAN LV device found on {} "
            "(core_frame={} observed_frames={} battery_marker={} device_marker={} "
            "identity_frame={} bslbatt_identity={})".format(
                can_interface,
                int(device_state.seen_core_frame),
                ",".join("0x{:03X}".format(can_id) for can_id in sorted(device_state.observed_frame_ids)) or "-",
                int(device_state.seen_battery_marker),
                int(device_state.seen_device_marker),
                int(device_state.seen_identity_frame),
                int(device_state.seen_bslbatt_identity),
            ),
        )
        return EXIT_OK

    key = "{}:{}".format(can_interface, device["node_id"])
    if key not in found:
        found.add(key)
        print_device(device, can_interface, args.product_id, args.type)

    return EXIT_OK


def list_devices(args):
    interfaces = [args.can] if args.can else list_available_can_interfaces()
    found = set()

    if not interfaces:
        debug(args.debug, "No can*/vecan* interfaces found")
        return EXIT_OK

    debug(args.debug, "Scanning CAN interfaces: {}".format(", ".join(interfaces)))
    result = EXIT_OK
    for can_interface in interfaces:
        interface_result = list_devices_on_interface(can_interface, args, found)
        if interface_result == EXIT_OK:
            continue
        if args.can:
            return interface_result
        result = interface_result

    return result


def build_parser():
    parser = argparse.ArgumentParser(description="List BSLBATT devices for Venus OS")
    parser.add_argument("-l", "--list", action="store_true", help="list updatable devices")
    parser.add_argument(
        "-c",
        "--can",
        help="CAN interface, for example can0 or vecan0; omitted means scan all can*/vecan* interfaces",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="passive discovery timeout in seconds")
    parser.add_argument("--product-id", default=PRODUCT_ID, help="Victron Product ID assigned by Victron")
    parser.add_argument("--type", default=MANUFACTURER_TYPE, help="manufacturer type used by VRM")
    parser.add_argument("-d", "--debug", action="store_true", help="write debug logs to stderr")
    return parser


def main():
    configure_stdout()
    args = build_parser().parse_args()
    if not args.list:
        return EXIT_ARGUMENT_ERROR
    if args.timeout <= 0:
        debug(args.debug, "timeout must be greater than zero")
        return EXIT_ARGUMENT_ERROR
    return list_devices(args)


if __name__ == "__main__":
    sys.exit(main() & 0xFF)
