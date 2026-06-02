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
import os
import re
import select
import socket
import struct
import sys
import time


EXIT_OK = 0
EXIT_GENERAL_ERROR = 1
EXIT_CAN_INIT_ERROR = 2
EXIT_CAN_COMM_ERROR = 3
EXIT_ARGUMENT_ERROR = 6

MANUFACTURER_TYPE = "bslbatt"
PRODUCT_ID = "TODO_PRODUCT_ID"
DEVICE_DESCRIPTION = "BSLBATT BMS"

CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_ID_MASK = 0x1FFFFFFF


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


def decode_bslbatt_device(frame):
    """
    Decode one passive CAN frame into a BSLBATT device record.

    Replace this function with the actual BSLBATT discovery/version protocol.
    Return None when the frame is not a BSLBATT identification frame.
    Return a dict with:
        node_id: integer CAN node/address used by the updater
        serial: user-visible serial number
        version: current firmware version string
        description: user-visible product description
    """
    return None


def list_available_can_interfaces():
    """
    Return available GX CAN interfaces matching Victron's can/vecan naming.

    This intentionally only inspects existing interfaces. It does not change
    bitrate, state, or any other CAN-bus setting.
    """
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        return []
    return sorted(name for name in names if re.match(r"^(can|vecan)[0-9]+$", name))


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
            if frame["is_error"] or frame["is_remote"]:
                continue

            device = decode_bslbatt_device(frame)
            if device is None:
                continue

            key = "{}:{}".format(can_interface, device["node_id"])
            if key in found:
                continue
            found.add(key)
            print_device(device, can_interface, args.product_id, args.type)
    finally:
        sock.close()

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
