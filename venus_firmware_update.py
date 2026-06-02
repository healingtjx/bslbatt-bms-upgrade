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
import zipfile


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
# oldcode uses APP_BMS_UPGRADE_ACK_BATCH_SIZE; BMS_CAN.xlsx states the BMS
# acknowledges once after every 10 firmware data frames.
BMS_UPGRADE_ACK_BATCH_SIZE = 10
BMS_UPGRADE_START_ACK_TIMEOUT_SECONDS = 5.0
BMS_UPGRADE_DATA_ACK_TIMEOUT_SECONDS = 10.0
BMS_UPGRADE_FINISH_ACK_TIMEOUT_SECONDS = 10.0
BMS_UPGRADE_FRAME_INTERVAL_SECONDS = 0.002


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
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True, write_through=True)


def debug(enabled, message):
    if enabled:
        print(message, file=sys.stderr, flush=True)


def xml_escape(value):
    return html.escape(str(value), quote=False)


def xml_message(text):
    print('<message type="normal">{}</message>'.format(xml_escape(text)), flush=True)


_last_progress_level = None


def xml_progress(level):
    global _last_progress_level
    level = max(0, min(100, int(level)))
    if level == _last_progress_level:
        return
    _last_progress_level = level
    print('<progress level="{}" />'.format(level), flush=True)


def parse_connection(connection):
    match = re.match(r"^socketcan:([^/]+)/(.+)$", connection)
    if not match:
        raise ValueError("unsupported connection: {}".format(connection))
    can_interface = match.group(1)
    if not re.match(r"^(can|vecan)[0-9]+$", can_interface):
        raise ValueError("unsupported CAN interface: {}".format(can_interface))
    node_id = int(match.group(2), 0)
    if node_id < 0 or node_id > CAN_ID_MASK:
        raise ValueError("node id must be between 0 and 0x{:X}".format(CAN_ID_MASK))
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


def read_firmware_from_zip(path):
    try:
        with zipfile.ZipFile(path, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise OSError("zip CRC check failed for {}".format(bad_member))

            candidates = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = os.path.basename(info.filename)
                if not name or name.startswith("."):
                    continue
                if name.lower().endswith((".bin", ".fw", ".img")):
                    candidates.append(info)

            if len(candidates) != 1:
                raise FirmwareError("zip package must contain exactly one .bin/.fw/.img firmware file")

            return archive.read(candidates[0])
    except zipfile.BadZipFile as exc:
        raise OSError("invalid zip file: {}".format(exc))


def read_firmware(path):
    if not os.path.isfile(path):
        raise OSError("firmware file does not exist: {}".format(path))
    if zipfile.is_zipfile(path):
        data = read_firmware_from_zip(path)
    else:
        with open(path, "rb") as firmware_file:
            data = firmware_file.read()
    if not data:
        raise OSError("firmware file is empty")
    return data


def crc32_hex(data):
    return "{:08X}".format(binascii.crc32(data) & 0xFFFFFFFF)


def le32(value):
    return struct.pack("<I", value & 0xFFFFFFFF)


def read_le32(data):
    if len(data) < 4:
        return 0
    return struct.unpack("<I", data[:4])[0]


def payload_hex(payload):
    return " ".join("{:02X}".format(byte) for byte in payload)


def can_id_name(can_id):
    names = {
        BMS_UPGRADE_START_REQ_ID: "START_REQ",
        BMS_UPGRADE_START_ACK_ID: "START_ACK",
        BMS_UPGRADE_DATA_ACK_ID: "DATA_ACK",
        BMS_UPGRADE_FINISH_REQ_ID: "FINISH_REQ",
        BMS_UPGRADE_FINISH_ACK_ID: "FINISH_ACK",
        BMS_UPGRADE_ERROR_ID: "ERROR",
    }
    if can_id in names:
        return names[can_id]
    if can_id >= BMS_UPGRADE_DATA_FRAME_BASE_ID and (can_id & 0xFF000000) == 0x13000000:
        return "DATA_FRAME"
    return "UNKNOWN"


def parse_control_payload(can_id, payload):
    if len(payload) != BMS_UPGRADE_FRAME_TOTAL_SIZE:
        return "invalid_len={}".format(len(payload))
    if can_id == BMS_UPGRADE_ERROR_ID:
        return "error_payload={}".format(payload.hex().upper())
    if payload == b"\xFF" * BMS_UPGRADE_FRAME_TOTAL_SIZE:
        return "all_ff=true"
    if can_id in (BMS_UPGRADE_START_ACK_ID, BMS_UPGRADE_FINISH_ACK_ID):
        return "image_size={} frame_count={}".format(read_le32(payload[:4]), read_le32(payload[4:8]))
    if can_id == BMS_UPGRADE_DATA_ACK_ID:
        return "acked_frame_count={} tail={}".format(read_le32(payload[:4]), payload[4:8].hex().upper())
    return "le32_0={} le32_4={}".format(read_le32(payload[:4]), read_le32(payload[4:8]))


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
    transfer_size = ceil_div(len(firmware), BMS_UPGRADE_FRAME_DATA_SIZE) * BMS_UPGRADE_FRAME_DATA_SIZE
    frame_count = transfer_size // BMS_UPGRADE_FRAME_DATA_SIZE
    if transfer_size > 0xFFFFFFFF or frame_count > 0xFFFFFFFF:
        raise FirmwareError("firmware is too large for BSLBATT CAN upgrade protocol")
    return {
        "size": len(firmware),
        "transfer_size": transfer_size,
        "frame_count": frame_count,
        "crc32": crc32_hex(firmware),
    }


class BslbattFirmwareUpdater:
    """
    BSLBATT BMS CAN upgrade protocol ported from oldcode/bms_upgrade_service.c.

    The Venus-facing contract is handled outside this class: command arguments,
    XML progress, line-buffered stdout, and standard exit codes.
    """

    def __init__(self, sock, node_id, firmware, firmware_info, debug_enabled=False, can_log_path=None):
        self.sock = sock
        self.node_id = node_id
        self.firmware = firmware
        self.firmware_info = firmware_info
        self.debug_enabled = debug_enabled
        self.can_log_path = can_log_path
        self.can_log_failed = False
        self.firmware_size = len(firmware)
        self.transfer_size = firmware_info["transfer_size"]
        self.frame_count = firmware_info["frame_count"]
        self.sent_frame_count = 0
        self.next_offset = 0
        self.last_frame_tail = b"\x00\x00\x00\x00"
        self.last_logged_progress = -1
        self.write_can_log(
            "START firmware_size={} transfer_size={} frame_count={} crc32={} node_id=0x{:X}".format(
                self.firmware_size,
                self.transfer_size,
                self.frame_count,
                self.firmware_info["crc32"],
                self.node_id,
            )
        )

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
                raw_frame = self.sock.recv(CAN_FRAME_SIZE)
            except OSError:
                return
            self.log_can_frame("RX_DRAIN", unpack_can_frame(raw_frame))

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
            self.log_can_frame("RX", frame, expected_can_id)
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
        self.debug("RX id=0x{:08X} len={} data={}".format(can_id, len(payload), payload_hex(payload)))

    def write_can_log(self, line):
        if not self.can_log_path or self.can_log_failed:
            return
        try:
            with open(self.can_log_path, "a") as log_file:
                log_file.write("{:.3f} {}\n".format(time.time(), line))
        except OSError as exc:
            self.can_log_failed = True
            self.debug("CAN log write failed: {}".format(exc))

    def log_can_frame(self, direction, frame, expected_can_id=None):
        can_id = frame["can_id"]
        payload = frame["data"]
        flags = []
        if frame["is_extended"]:
            flags.append("EFF")
        else:
            flags.append("SFF")
        if frame["is_remote"]:
            flags.append("RTR")
        if frame["is_error"]:
            flags.append("ERR")
        parsed = ""
        if frame["is_extended"] and len(payload) > 0:
            parsed = " parsed={}".format(parse_control_payload(can_id, payload))
        expected = ""
        if expected_can_id is not None:
            expected = " expected=0x{:08X}".format(expected_can_id)
        self.write_can_log(
            "{} id=0x{:08X} name={} len={} flags={} data={}{}{}".format(
                direction,
                can_id,
                can_id_name(can_id),
                len(payload),
                ",".join(flags) if flags else "-",
                payload_hex(payload),
                expected,
                parsed,
            )
        )

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
                self.log_transfer_progress("SEND")
                if BMS_UPGRADE_FRAME_INTERVAL_SECONDS > 0:
                    time.sleep(BMS_UPGRADE_FRAME_INTERVAL_SECONDS)

            self.wait_data_ack(batch_target)
            self.log_transfer_progress("ACK")

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
            if payload == b"\xFF" * BMS_UPGRADE_FRAME_TOTAL_SIZE:
                return
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

    def log_transfer_progress(self, stage):
        percent = int((self.sent_frame_count * 100) / max(1, self.frame_count))
        if stage != "ACK" and percent == self.last_logged_progress:
            return
        self.last_logged_progress = percent
        self.write_can_log(
            "PROGRESS stage={} sent_frames={}/{} sent_bytes={}/{} percent={} next_offset={}".format(
                stage,
                self.sent_frame_count,
                self.frame_count,
                min(self.next_offset, self.firmware_size),
                self.firmware_size,
                percent,
                self.next_offset,
            )
        )

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
        xml_message("Invalid arguments")
        debug(args.debug, str(exc))
        return EXIT_ARGUMENT_ERROR

    if not os.path.isabs(args.file):
        xml_message("Invalid arguments")
        debug(args.debug, "firmware file path must be absolute: {}".format(args.file))
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

    updater = BslbattFirmwareUpdater(sock, node_id, firmware, firmware_info, args.debug, args.can_log)
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
    finally:
        sock.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Update BSLBATT firmware for Venus OS")
    parser.add_argument("--update", action="store_true", help="update firmware")
    parser.add_argument("-s", "--connection", required=True, help="connection from list XML, for example socketcan:can0/0x2A")
    parser.add_argument("-f", "--file", required=True, help="firmware file absolute path")
    parser.add_argument("-d", "--debug", action="store_true", help="write debug logs to stderr")
    parser.add_argument(
        "--can-log",
        default="venus_firmware_update_can.log",
        help="local file for parsed CAN RX logs; use an empty value to disable",
    )
    return parser


def main():
    configure_stdout()
    args = build_parser().parse_args()
    if not args.update:
        return EXIT_ARGUMENT_ERROR
    return update(args)


if __name__ == "__main__":
    sys.exit(main() & 0xFF)
