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
        sys.stdout.reconfigure(line_buffering=True)


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


def list_devices(args):
    try:
        sock = open_can(args.can)
    except OSError as exc:
        debug(args.debug, "CAN init failed on {}: {}".format(args.can, exc))
        return EXIT_CAN_INIT_ERROR

    deadline = time.monotonic() + args.timeout
    found = set()
    debug(args.debug, "Listening on {} for {:.1f}s".format(args.can, args.timeout))

    while time.monotonic() < deadline:
        wait = min(0.2, max(0.0, deadline - time.monotonic()))
        try:
            readable, _, _ = select.select([sock], [], [], wait)
        except OSError as exc:
            debug(args.debug, "CAN select failed: {}".format(exc))
            return EXIT_CAN_COMM_ERROR

        if not readable:
            continue

        try:
            raw_frame = sock.recv(CAN_FRAME_SIZE)
        except OSError as exc:
            debug(args.debug, "CAN receive failed: {}".format(exc))
            return EXIT_CAN_COMM_ERROR

        frame = unpack_can_frame(raw_frame)
        if frame["is_error"] or frame["is_remote"]:
            continue

        device = decode_bslbatt_device(frame)
        if device is None:
            continue

        key = "{}:{}".format(args.can, device["node_id"])
        if key in found:
            continue
        found.add(key)
        print_device(device, args.can, args.product_id, args.type)

    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(description="List BSLBATT devices for Venus OS")
    parser.add_argument("--list", action="store_true", help="list updatable devices")
    parser.add_argument("-c", "--can", required=True, help="CAN interface, for example can0 or vecan0")
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
    sys.exit(main())
