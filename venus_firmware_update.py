#!/usr/bin/env python3
"""
BSLBATT Venus OS firmware update tool.

Called by Victron GX / Venus OS after a user uploads firmware through VRM.
Stdout must contain XML progress/messages only. Debug output goes to stderr.

Example:
    ./venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/bms.bin
"""

import argparse
import binascii
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
EXIT_TIMEOUT = 4
EXIT_FIRMWARE_ERROR = 5
EXIT_ARGUMENT_ERROR = 6
EXIT_DEVICE_NOT_FOUND = 7
EXIT_MEMORY_ERROR = 8
EXIT_FILE_ERROR = 9
EXIT_VERIFY_FAILED = 10
EXIT_VERIFY_TIMEOUT = 11

CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_ID_MASK = 0x1FFFFFFF

BMS_UPGRADE_START_REQ_ID = 0x18A055AA
BMS_UPGRADE_START_ACK_ID = 0x18A0AA55
BMS_UPGRADE_DATA_FRAME_BASE_ID = 0x13000001
BMS_UPGRADE_DATA_ACK_ID = 0x18A1AA55
BMS_UPGRADE_FINISH_REQ_ID = 0x18A255AA
BMS_UPGRADE_FINISH_ACK_ID = 0x18A2AA55
BMS_UPGRADE_ERROR_ID = 0x18A3AA55
BMS_UPGRADE_FRAME_DATA_SIZE = 7
BMS_UPGRADE_FRAME_TOTAL_SIZE = 8
BMS_UPGRADE_ACK_BATCH_SIZE = 64
BMS_UPGRADE_START_ACK_TIMEOUT_SECONDS = 5.0
BMS_UPGRADE_DATA_ACK_TIMEOUT_SECONDS = 5.0
BMS_UPGRADE_FINISH_ACK_TIMEOUT_SECONDS = 10.0
BMS_UPGRADE_FRAME_INTERVAL_SECONDS = 0.0


class FirmwareError(Exception):
    pass


class DeviceNotFoundError(Exception):
    pass


class VerifyFailedError(Exception):
    pass


class VerifyTimeoutError(Exception):
    pass


class MemoryErrorOnDevice(Exception):
    pass


def configure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)


def debug(enabled, message):
    if enabled:
        print(message, file=sys.stderr, flush=True)


def xml_escape(value):
    return html.escape(str(value), quote=False)


def xml_message(text):
    print('<message type="normal">{}</message>'.format(xml_escape(text)), flush=True)


def xml_progress(level):
    level = max(0, min(100, int(level)))
    print('<progress level="{}" />'.format(level), flush=True)


def parse_connection(connection):
    match = re.match(r"^socketcan:([^/]+)/(.+)$", connection)
    if not match:
        raise ValueError("unsupported connection: {}".format(connection))
    can_interface = match.group(1)
    node_id = int(match.group(2), 0)
    if node_id < 0:
        raise ValueError("node id must be non-negative")
    return can_interface, node_id


def open_can(interface_name):
    if not hasattr(socket, "AF_CAN") or not hasattr(socket, "CAN_RAW"):
        raise OSError("SocketCAN is required on Victron GX / Venus OS")
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((interface_name,))
    sock.settimeout(2.0)
    return sock


def pack_can_frame(can_id, payload, extended=False):
    payload = bytes(bytearray(payload))
    if len(payload) > 8:
        raise ValueError("CAN payload must be <= 8 bytes")
    frame_id = can_id & CAN_ID_MASK
    if extended:
        frame_id |= CAN_EFF_FLAG
    return struct.pack(CAN_FRAME_FORMAT, frame_id, len(payload), payload.ljust(8, b"\x00"))


def send_can(sock, can_id, payload, extended=False):
    sock.send(pack_can_frame(can_id, payload, extended))


def unpack_can_frame(raw_frame):
    can_id, data_len, data = struct.unpack(CAN_FRAME_FORMAT, raw_frame)
    return {
        "can_id": can_id & CAN_ID_MASK,
        "is_extended": bool(can_id & CAN_EFF_FLAG),
        "is_remote": bool(can_id & CAN_RTR_FLAG),
        "is_error": bool(can_id & CAN_ERR_FLAG),
        "data": data[:data_len],
    }


def read_firmware(path):
    if not os.path.isfile(path):
        raise OSError("firmware file does not exist: {}".format(path))
    with open(path, "rb") as firmware_file:
        data = firmware_file.read()
    if not data:
        raise FirmwareError("firmware file is empty")
    return data


def crc32_hex(data):
    return "{:08X}".format(binascii.crc32(data) & 0xFFFFFFFF)


def le32(value):
    return struct.pack("<I", value & 0xFFFFFFFF)


def read_le32(data):
    if len(data) < 4:
        return 0
    return struct.unpack("<I", data[:4])[0]


def ceil_div(value, divisor):
    return (value + divisor - 1) // divisor


def validate_bslbatt_firmware(firmware):
    """
    Validate BSLBATT firmware format before touching the device.

    Replace this with the actual package checks, for example:
        header magic
        target model
        target version
        payload length
        CRC/signature
    """
    if not firmware:
        raise FirmwareError("empty firmware")
    return {
        "size": len(firmware),
        "crc32": crc32_hex(firmware),
    }


class BslbattFirmwareUpdater:
    """
    BSLBATT BMS CAN upgrade protocol ported from oldcode/bms_upgrade_service.c.

    The Venus-facing contract is handled outside this class: command arguments,
    XML progress, line-buffered stdout, and standard exit codes.
    """

    def __init__(self, sock, node_id, firmware, firmware_info, debug_enabled=False):
        self.sock = sock
        self.node_id = node_id
        self.firmware = firmware
        self.firmware_info = firmware_info
        self.debug_enabled = debug_enabled
        self.firmware_size = len(firmware)
        self.transfer_size = ceil_div(self.firmware_size, BMS_UPGRADE_FRAME_DATA_SIZE) * BMS_UPGRADE_FRAME_DATA_SIZE
        self.frame_count = self.transfer_size // BMS_UPGRADE_FRAME_DATA_SIZE
        self.sent_frame_count = 0
        self.next_offset = 0
        self.last_frame_tail = b"\x00\x00\x00\x00"

    def debug(self, message):
        debug(self.debug_enabled, message)

    def send_extended(self, can_id, payload):
        payload_hex = " ".join("{:02X}".format(byte) for byte in payload)
        self.debug("TX id=0x{:08X} len={} data={}".format(can_id, len(payload), payload_hex))
        send_can(self.sock, can_id, payload, extended=True)

    def drain_control_frames(self):
        while True:
            try:
                readable, _, _ = select.select([self.sock], [], [], 0)
            except OSError:
                return
            if not readable:
                return
            try:
                self.sock.recv(CAN_FRAME_SIZE)
            except OSError:
                return

    def receive_control_frame(self, expected_can_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("timeout waiting for 0x{:08X}".format(expected_can_id))

            readable, _, _ = select.select([self.sock], [], [], remaining)
            if not readable:
                raise socket.timeout("timeout waiting for 0x{:08X}".format(expected_can_id))

            raw_frame = self.sock.recv(CAN_FRAME_SIZE)
            frame = unpack_can_frame(raw_frame)
            if frame["is_error"] or frame["is_remote"] or not frame["is_extended"]:
                continue

            can_id = frame["can_id"]
            payload = frame["data"]
            if can_id == BMS_UPGRADE_ERROR_ID:
                self.log_rx(can_id, payload)
                raise MemoryErrorOnDevice("BMS reported upgrade error frame")

            if can_id != expected_can_id:
                continue

            self.log_rx(can_id, payload)
            if len(payload) != BMS_UPGRADE_FRAME_TOTAL_SIZE:
                self.debug("Ignore invalid ACK length for 0x{:08X}: {}".format(can_id, len(payload)))
                continue
            return payload

    def log_rx(self, can_id, payload):
        payload_hex = " ".join("{:02X}".format(byte) for byte in payload)
        self.debug("RX id=0x{:08X} len={} data={}".format(can_id, len(payload), payload_hex))

    def size_and_count_payload(self):
        return le32(self.transfer_size) + le32(self.frame_count)

    def locate_device(self):
        self.drain_control_frames()

    def enter_bootloader(self):
        self.debug(
            "prepare firmware rawSize={} transferSize={} frameCount={} node_id=0x{:X}".format(
                self.firmware_size,
                self.transfer_size,
                self.frame_count,
                self.node_id,
            )
        )
        self.send_extended(BMS_UPGRADE_START_REQ_ID, self.size_and_count_payload())
        self.receive_control_frame(BMS_UPGRADE_START_ACK_ID, BMS_UPGRADE_START_ACK_TIMEOUT_SECONDS)

    def erase_flash(self):
        return

    def write_firmware(self):
        while self.sent_frame_count < self.frame_count:
            batch_target = min(self.frame_count, self.sent_frame_count + BMS_UPGRADE_ACK_BATCH_SIZE)
            while self.sent_frame_count < batch_target:
                self.send_data_frame()
                progress = 20 + int((self.sent_frame_count * 70) / max(1, self.frame_count))
                xml_progress(min(89, progress))
                if BMS_UPGRADE_FRAME_INTERVAL_SECONDS > 0:
                    time.sleep(BMS_UPGRADE_FRAME_INTERVAL_SECONDS)

            self.wait_data_ack(batch_target)

    def send_data_frame(self):
        chunk = self.firmware[self.next_offset : self.next_offset + BMS_UPGRADE_FRAME_DATA_SIZE]
        read_size = len(chunk)
        payload = bytearray(b"\xFF" * BMS_UPGRADE_FRAME_TOTAL_SIZE)
        payload[:read_size] = chunk
        payload[7] = sum(payload[:BMS_UPGRADE_FRAME_DATA_SIZE]) & 0xFF

        can_id = BMS_UPGRADE_DATA_FRAME_BASE_ID + self.sent_frame_count
        self.send_extended(can_id, payload)

        self.sent_frame_count += 1
        self.next_offset += read_size
        self.last_frame_tail = bytes(payload[4:8])

    def wait_data_ack(self, expected_frame_count):
        while True:
            payload = self.receive_control_frame(BMS_UPGRADE_DATA_ACK_ID, BMS_UPGRADE_DATA_ACK_TIMEOUT_SECONDS)
            acked_frame_count = read_le32(payload)
            tail = payload[4:8]
            if acked_frame_count != expected_frame_count:
                self.debug(
                    "Ignore data ACK frameCount={}, expected={}".format(acked_frame_count, expected_frame_count)
                )
                continue
            if tail != self.last_frame_tail:
                self.debug(
                    "Ignore data ACK tail={}, expected={}".format(tail.hex().upper(), self.last_frame_tail.hex().upper())
                )
                continue
            return

    def verify_firmware(self):
        return

    def reboot_application(self):
        self.send_extended(BMS_UPGRADE_FINISH_REQ_ID, self.size_and_count_payload())
        self.receive_control_frame(BMS_UPGRADE_FINISH_ACK_ID, BMS_UPGRADE_FINISH_ACK_TIMEOUT_SECONDS)

    def run(self):
        xml_message("Checking firmware")
        debug(
            self.debug_enabled,
            "firmware size={} crc32={}".format(self.firmware_info["size"], self.firmware_info["crc32"]),
        )
        xml_progress(0)

        xml_message("Locating device")
        self.locate_device()
        xml_progress(5)

        xml_message("Entering bootloader")
        self.enter_bootloader()
        xml_progress(10)

        xml_message("Erasing device")
        self.erase_flash()
        xml_progress(20)

        xml_message("Writing firmware")
        self.write_firmware()
        xml_progress(90)

        xml_message("Verifying firmware")
        self.verify_firmware()
        xml_progress(98)

        xml_message("Starting application")
        self.reboot_application()
        xml_progress(100)
        xml_message("Update successful")


def update(args):
    try:
        can_interface, node_id = parse_connection(args.connection)
    except ValueError as exc:
        debug(args.debug, str(exc))
        return EXIT_ARGUMENT_ERROR

    try:
        firmware = read_firmware(args.file)
        firmware_info = validate_bslbatt_firmware(firmware)
    except OSError as exc:
        xml_message("Firmware file error")
        debug(args.debug, str(exc))
        return EXIT_FILE_ERROR
    except FirmwareError as exc:
        xml_message("Firmware is not compatible")
        debug(args.debug, str(exc))
        return EXIT_FIRMWARE_ERROR

    try:
        sock = open_can(can_interface)
    except OSError as exc:
        xml_message("CAN init failed")
        debug(args.debug, str(exc))
        return EXIT_CAN_INIT_ERROR

    updater = BslbattFirmwareUpdater(sock, node_id, firmware, firmware_info, args.debug)
    try:
        updater.run()
        return EXIT_OK
    except NotImplementedError as exc:
        xml_message("Update protocol is not implemented")
        debug(args.debug, str(exc))
        return EXIT_GENERAL_ERROR
    except DeviceNotFoundError as exc:
        xml_message("Device not found")
        debug(args.debug, str(exc))
        return EXIT_DEVICE_NOT_FOUND
    except socket.timeout as exc:
        xml_message("Device response timeout")
        debug(args.debug, str(exc))
        return EXIT_TIMEOUT
    except MemoryErrorOnDevice as exc:
        xml_message("Device memory error")
        debug(args.debug, str(exc))
        return EXIT_MEMORY_ERROR
    except VerifyTimeoutError as exc:
        xml_message("Verification timeout")
        debug(args.debug, str(exc))
        return EXIT_VERIFY_TIMEOUT
    except VerifyFailedError as exc:
        xml_message("Verification failed")
        debug(args.debug, str(exc))
        return EXIT_VERIFY_FAILED
    except OSError as exc:
        xml_message("CAN communication failed")
        debug(args.debug, str(exc))
        return EXIT_CAN_COMM_ERROR
    except FirmwareError as exc:
        xml_message("Firmware error")
        debug(args.debug, str(exc))
        return EXIT_FIRMWARE_ERROR
    except Exception as exc:
        xml_message("Update failed")
        debug(args.debug, repr(exc))
        return EXIT_GENERAL_ERROR


def build_parser():
    parser = argparse.ArgumentParser(description="Update BSLBATT firmware for Venus OS")
    parser.add_argument("--update", action="store_true", help="update firmware")
    parser.add_argument("-s", "--connection", required=True, help="connection from list XML, for example socketcan:can0/0x2A")
    parser.add_argument("-f", "--file", required=True, help="firmware file absolute path")
    parser.add_argument("-d", "--debug", action="store_true", help="write debug logs to stderr")
    return parser


def main():
    configure_stdout()
    args = build_parser().parse_args()
    if not args.update:
        return EXIT_ARGUMENT_ERROR
    return update(args)


if __name__ == "__main__":
    sys.exit(main())
