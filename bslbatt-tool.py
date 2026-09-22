#!/usr/bin/env python3
"""
BSLBATT Venus OS device discovery and firmware update tool.

Called by Victron GX / Venus OS to list updatable devices and to update
firmware after a user uploads firmware through VRM. Stdout must contain the
Venus OS XML contract only. Debug output goes to stderr.

中文阅读提示：
    这个脚本合并了设备列表和固件升级两个入口。标准输出 stdout 只能打印
    Venus OS 能识别的 XML；调试日志必须走 stderr，避免污染 XML。
English note:
    This script combines device listing and firmware update entry points.
    Stdout must only print XML recognized by Venus OS; debug logs must go to
    stderr to avoid contaminating the XML output.

Example:
    ./bslbatt-tool.py -c can0
    ./bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/bms.bin
"""

import argparse
import html
import os
import re
import select
import socket
import struct
import subprocess
import sys
import time
import zipfile
from types import SimpleNamespace


# Venus OS / 调用方通过退出码判断失败类型；0 表示成功。
# Venus OS / callers determine the failure type from the exit code; 0 means success.
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

MANUFACTURER_TYPE = "bslbatt"
PRODUCT_ID = "0xB021"
DEVICE_DESCRIPTION = "BSLBATT BMS"
DEVICE_FALLBACK_NAME = "BSLBATT"
DEFAULT_NODE_ID = 0
DEFAULT_NODE_ID_TEXT = "0x{:X}".format(DEFAULT_NODE_ID)

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

# Linux SocketCAN 原始 CAN 帧结构：
# Linux SocketCAN raw CAN frame structure:
#   can_id: 4 字节
#   can_id: 4 bytes
#   can_dlc: 1 字节
#   can_dlc: 1 byte
#   padding: 3 字节
#   padding: 3 bytes
#   data: 8 字节
#   data: 8 bytes
CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)

# SocketCAN 在 can_id 高位中携带扩展帧、远程帧、错误帧标志。
# SocketCAN carries extended, remote, and error frame flags in the high bits of can_id.
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_ID_MASK = 0x1FFFFFFF

BMS_CAN_CORE_IDS = (CAN_ID_LIMITS, CAN_ID_SOC, CAN_ID_MEASUREMENTS, CAN_ID_ALARMS)
BMS_CAN_OBSERVED_IDS = (CAN_ID_STATUS_OBSERVED, CAN_ID_FLAGS_OBSERVED, CAN_ID_CAPACITY_OBSERVED)


class FirmwareError(Exception):
    """固件包内容或格式不符合升级协议。
    The firmware package content or format does not match the update protocol."""

    pass


class DeviceIdError(ValueError):
    """VRM 回传的 connection-id / -n 与当前单设备工具不匹配。
    The connection-id / -n returned by VRM does not match this single-device tool."""

    pass


def configure_stdout():
    """配置 stdout/stderr 为行缓冲，确保 Venus OS 能及时收到 XML 进度。
    Configure stdout/stderr as line-buffered so Venus OS receives XML progress promptly."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True, write_through=True)


def debug(enabled, message):
    """调试日志只写 stderr，避免破坏 stdout 上的 XML 协议。
    Write debug logs only to stderr to avoid breaking the XML protocol on stdout."""
    if enabled:
        print(message, file=sys.stderr, flush=True)


def xml_escape(value):
    """对 XML 文本内容做转义，避免消息里出现 <、& 等字符导致 XML 非法。
    Escape XML text content so characters such as < and & do not make the XML invalid."""
    return html.escape(str(value), quote=False)


def xml_message(text):
    """向 Venus OS 输出普通消息。stdout 上只能出现这类 XML。
    Output a normal message to Venus OS. Only this XML should appear on stdout."""
    print('<message type="normal">{}</message>'.format(xml_escape(text)), flush=True)


_last_progress_level = None


def xml_progress(level):
    """向 Venus OS 输出进度；相同进度不会重复打印，减少 stdout 噪声。
    Output progress to Venus OS; identical levels are not repeated to reduce stdout noise."""
    global _last_progress_level
    level = max(0, min(100, int(level)))
    if level == _last_progress_level:
        return
    _last_progress_level = level
    print('<progress level="{}" />'.format(level), flush=True)


def xml_attr(value):
    """对 XML 属性值做转义。
    Escape XML attribute values."""
    return html.escape(str(value), quote=True)


def read_le16(data, offset):
    """从 payload 指定偏移读取小端 16 位整数；长度不足时返回 None。
    Read a little-endian 16-bit integer from payload at offset; return None if too short."""
    if len(data) < offset + 2:
        return None
    return data[offset] | (data[offset + 1] << 8)


def decode_battery_info_version(payload):
    """0x35F 的 BYTE2/BYTE3 表示固件版本，例如 02 03 12 45 => 12.45。
    BYTE2/BYTE3 of 0x35F represent the firmware version, for example 02 03 12 45 => 12.45."""
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
    """拼接多个 CAN ASCII 字段，忽略空字段。
    Join multiple CAN ASCII fields while ignoring empty fields."""
    return "".join(part for part in parts if part).strip()


def contains_bslbatt(value):
    """判断身份字段中是否包含 BSLBATT。
    Check whether the identity field contains BSLBATT."""
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
            parts.append(DEVICE_FALLBACK_NAME)
        return " ".join(parts) if parts else DEVICE_DESCRIPTION

    def _fallback_serial(self):
        parts = []
        if self.manufacturer:
            parts.append(self.manufacturer)
        if self.family:
            parts.append(self.family)
        if self.model is not None:
            parts.append(DEVICE_FALLBACK_NAME)
        parts.append(self.can_interface)
        return "-".join(parts)


def validate_can_interface(can_interface):
    """校验 Venus OS 传入或扫描出的 CAN 接口名。
    Validate a CAN interface name passed by Venus OS or discovered by scanning."""
    if not re.match(r"^(can|vecan)[0-9]+$", can_interface):
        raise ValueError("unsupported CAN interface: {}".format(can_interface))
    return can_interface


def parse_node_id(value):
    """解析 VRM 回传的 CAN connection-id / -n 参数。
    Parse the CAN connection-id / -n parameter returned by VRM."""
    try:
        node_id = int(str(value), 0)
    except (TypeError, ValueError):
        raise DeviceIdError("unsupported node id: {}".format(value))
    if node_id < 0 or node_id > CAN_ID_MASK:
        raise DeviceIdError("node id must be between 0 and 0x{:X}".format(CAN_ID_MASK))
    return node_id


def validate_node_id(node_id):
    """BSLBATT 当前 CAN 升级协议固定为单设备，VRM 标识必须匹配列表输出。
    The current BSLBATT CAN update protocol is fixed to one device, so the VRM identity must match list output."""
    if node_id != DEFAULT_NODE_ID:
        raise DeviceIdError(
            "unsupported node id: 0x{:X}; this tool only supports {}".format(node_id, DEFAULT_NODE_ID_TEXT)
        )
    return node_id


def parse_connection(connection):
    """解析旧版连接字符串，例如 socketcan:can0/0x2A；新 VRM 调用优先使用 -c/-n。
    Parse a legacy connection string such as socketcan:can0/0x2A; newer VRM calls should prefer -c/-n."""
    match = re.match(r"^socketcan:([^/]+)/(.+)$", connection)
    if not match:
        raise ValueError("unsupported connection: {}".format(connection))
    can_interface = validate_can_interface(match.group(1))
    node_id = validate_node_id(parse_node_id(match.group(2)))
    return can_interface, node_id


def resolve_update_target(args):
    """
    Resolve the target selected by VRM.

    mqtt-rpc/ThirdPartyUpdater passes the XML connection string back as -c,
    for example socketcan:can0/0x0. Also keep supporting the explicit
    -c can0 -n 0x0 form and the legacy --connection option.
    """
    if args.can and args.can.startswith("socketcan:"):
        return parse_connection(args.can)

    if args.can or args.node_id is not None:
        if not args.can or args.node_id is None:
            raise ValueError("--update requires both -c/--can and -n/--node-id")
        return validate_can_interface(args.can), validate_node_id(parse_node_id(args.node_id))

    if args.connection:
        return parse_connection(args.connection)

    raise ValueError("--update requires -c/--can and -n/--node-id")


def open_can(interface_name):
    """打开 Linux SocketCAN RAW 套接字并绑定到 can0/vecan0 等接口。
    Open a Linux SocketCAN RAW socket and bind it to an interface such as can0/vecan0."""
    if not hasattr(socket, "AF_CAN") or not hasattr(socket, "CAN_RAW"):
        raise OSError("SocketCAN is required on Victron GX / Venus OS")
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.bind((interface_name,))
        sock.settimeout(2.0)
    except BaseException:
        sock.close()
        raise
    return sock


def payload_hex(payload):
    """Format CAN payloads for discovery diagnostics."""
    return " ".join("{:02X}".format(byte) for byte in payload)


def unpack_can_frame(raw_frame):
    """把 SocketCAN 收到的原始帧拆成便于日志和协议判断的字典。
    Unpack a raw SocketCAN frame into a dictionary for logging and protocol checks."""
    can_id, data_len, data = struct.unpack(CAN_FRAME_FORMAT, raw_frame)
    return {
        "can_id": can_id & CAN_ID_MASK,
        "is_extended": bool(can_id & CAN_EFF_FLAG),
        "is_remote": bool(can_id & CAN_RTR_FLAG),
        "is_error": bool(can_id & CAN_ERR_FLAG),
        "data": data[:data_len],
    }


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
    """按 Venus OS 设备列表 XML 格式输出单个设备。
    Output one device using the Venus OS device-list XML format."""
    node_id = int(device["node_id"])
    connection_id = "0x{:X}".format(node_id)
    connection = "socketcan:{}/0x{:X}".format(can_interface, node_id)
    print(
        '<device serial="{serial}" version="{version}" description="{description}" '
        'id="{product_id}" type="{manufacturer_type}" '
        'connection-type="can" connection-id="{connection_id}" connection="{connection}" '
        'updatable="True" />'.format(
            serial=xml_attr(device["serial"]),
            version=xml_attr(device["version"]),
            description=xml_attr(device.get("description", DEVICE_DESCRIPTION)),
            product_id=xml_attr(product_id),
            manufacturer_type=xml_attr(manufacturer_type),
            connection_id=xml_attr(connection_id),
            connection=xml_attr(connection),
        ),
        flush=True,
    )


def list_devices_on_interface(can_interface, args, found):
    """在一个 CAN 接口上被动监听 BMS-CAN LV 帧并输出可升级设备。
    Passively listen for BMS-CAN LV frames on one CAN interface and output updatable devices."""
    try:
        sock = open_can(can_interface)
        sock.setblocking(False)
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
        print_device(device, can_interface, PRODUCT_ID, MANUFACTURER_TYPE)

    return EXIT_OK


def list_devices(args):
    """执行 --list：扫描指定或全部 CAN 接口并输出设备 XML。
    Run --list: scan the specified or all CAN interfaces and output device XML."""
    if not PRODUCT_ID:
        debug(args.debug, "Victron product id is required; set PRODUCT_ID in bslbatt-tool.py")
        return EXIT_ARGUMENT_ERROR

    try:
        interfaces = [validate_can_interface(args.can)] if args.can else list_available_can_interfaces()
    except ValueError as exc:
        debug(args.debug, str(exc))
        return EXIT_ARGUMENT_ERROR
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


def read_firmware_from_zip(path):
    """从 zip 固件包中读取唯一的 .bin/.fw/.img 文件，并先做 zip CRC 检查。
    Read the single .bin/.fw/.img file from a zip firmware package after checking the zip CRC."""
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
    """读取固件文件；支持直接读取二进制文件，也支持读取 zip 包中的固件。
    Read a firmware file; supports both direct binary files and firmware stored inside a zip package."""
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


# Wire settings and pacing matched to the captured CAN Update log.
UPGRADE_CONFIG = {
    'block_size_includes_number': False,
    'block_size_field': 'zero',
    'block_data_id_increment': False,
    'block_crc_number_padding': False,
    'block_control_padding': True,
    'firmware_size_padding': True,
    'final_control_padding': True,
    'firmware_size_endian': 'little',
    'response_size_endian': 'big',
    'block_number_endian': 'little',
    'field_endian': 'little',
    'block_crc_order': 'data_only',
    'block_crc_endian': 'little',
    'crc_endian': 'little',
    'tail_padding': 'ff',
    'tail_size': 'actual',
    'block_crc_scope': 'padded',
    'firmware_crc_scope': 'actual',
    'frame_interval': 0.002,
    'size_ack_delay': 0.048,
    'block_ack_delay': 0.047,
    'verify_delay': 0.032,
    'restart_delay': 0.032,
    'restart_settle_delay': 15.0,
    'ack_timeout': 300.0,
}

FRAME = struct.Struct('=IB3x8s')
EFF, RTR, ERR, MASK = 0x80000000, 0x40000000, 0x20000000, 0x1FFFFFFF
REQUESTS = {0x4610: 'SIZE', 0x4630: 'BLOCK_NUMBER', 0x4650: 'BLOCK_DATA',
            0x4670: 'BLOCK_CRC_SIZE', 0x4690: 'FIRMWARE_CRC',
            0x46B0: 'RESTART', 0x46D0: 'STATUS_QUERY'}
RESPONSES = {0x4621: 'SIZE_ACK', 0x4681: 'BLOCK_ACK', 0x46A1: 'CRC_ACK',
             0x46C1: 'RESTART_ACK', 0x46E1: 'STATUS_ACK'}
CODES = {
    0xA1: 'Firmware size accepted', 0xA2: 'Block accepted',
    0xA3: 'Firmware CRC accepted',
    1: 'Invalid firmware size', 2: 'Block CRC mismatch',
    3: 'Invalid block sequence', 4: 'Block write failed',
    5: 'Invalid block size', 6: 'CRC write failed',
    7: 'Firmware total size mismatch', 8: 'Firmware CRC mismatch',
    9: 'Invalid firmware', 10: 'Forwarding', 11: 'Local update started',
    12: 'Forwarding', 13: 'Update successful', 14: 'Forwarding failed',
    15: 'Update failed', 16: 'Local update in progress',
    17: 'Update conditions not met', 18: 'Incompatible device version',
    19: 'Invalid command sequence', 20: 'Firmware CRC16 storage failed',
    21: 'Update conditions not met',
}
VALID_CODES = {0x4621: {0xA1, 1}, 0x4681: {0xA2, 2, 3, 4, 5, 19},
               0x46A1: {0xA3, 6, 7, 8, 19, 20},
               0x46C1: {9, 10, 11, 19, 21}, 0x46E1: set(range(12, 20))}


def validate_bslbatt_firmware(firmware):
    if not 0 < len(firmware) <= 65535 * 128:
        raise FirmwareError('firmware must be nonempty and fit 65535 blocks')


class ProtocolError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def crc16(data):
    """CRC16/Modbus: init FFFF, reflected polynomial A001, xorout 0."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def median(values):
    """Small timing samples; support stripped-down target Python installations."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def unpack_frame(raw, direction='RX'):
    if len(raw) != FRAME.size:
        raise ValueError('invalid classic CAN frame size: {}'.format(len(raw)))
    identifier, dlc, payload = FRAME.unpack(raw)
    if dlc > 8:
        raise ValueError('invalid classic CAN DLC: {}'.format(dlc))
    return dict(id=identifier & MASK, extended=bool(identifier & EFF),
                remote=bool(identifier & RTR), error=bool(identifier & ERR),
                data=payload[:dlc], direction=direction)


def response_payload(identifier, data):
    """Accept logical payload or an 8-byte frame with zero-only trailing padding."""
    expected = 3 if data and (identifier, data[0]) in ((0x4621, 0xA1), (0x46A1, 0xA3)) else 1
    if len(data) == expected:
        return data
    if len(data) == 8 and not any(data[expected:]):
        return data[:expected]
    raise ValueError('invalid_length_or_padding={} expected={} or zero-padded DLC=8'.format(len(data), expected))


def decode(identifier, data):
    name = REQUESTS.get(identifier, RESPONSES.get(identifier, ''))
    if 0x4651 <= identifier <= 0x465F:
        name = 'BLOCK_DATA experimental_index={}'.format(identifier - 0x4650)
    if identifier in RESPONSES:
        raw_length = len(data)
        try:
            data = response_payload(identifier, data)
        except ValueError as exc:
            return name + ' ' + str(exc)
        if raw_length > len(data):
            name += ' zero_padding={}'.format(raw_length - len(data))
        return '{} status=0x{:02X} {}{}'.format(
            name, data[0], CODES.get(data[0], 'Unknown status'),
            ' extra=' + data[1:].hex(' ') if len(data) > 1 else '')
    sizes = {0x4610: 4, 0x4630: 2, 0x4670: 4, 0x4690: 2, 0x46B0: 0, 0x46D0: 0}
    if identifier in sizes and len(data) != sizes[identifier]:
        if len(data) == 8 and not any(data[sizes[identifier]:]):
            return name + ' zero_padding={}'.format(8 - sizes[identifier])
        return '{} invalid_length={} expected={}'.format(name, len(data), sizes[identifier])
    return name


class Logger:
    def __init__(self, path=None, debug_enabled=False):
        self.file = None
        self.quiet_frames = True
        self.debug_enabled = debug_enabled
        if path:
            try:
                self.file = open(path, 'a', encoding='utf-8')
            except OSError as exc:
                print('CAN log unavailable: {}'.format(exc), file=sys.stderr, flush=True)

    @property
    def frames_enabled(self):
        return self.file is not None or (self.debug_enabled and not self.quiet_frames)

    def persist(self, text):
        if self.file:
            try:
                self.file.write(text)
                self.file.flush()
            except OSError as exc:
                print('CAN log disabled: {}'.format(exc), file=sys.stderr, flush=True)
                self.close()

    def write(self, message):
        now = time.time()
        line = '{}.{:03d} {}'.format(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
                                    int(now * 1000) % 1000, message)
        debug(self.debug_enabled, line)
        if self.file:
            self.persist(line + '\n')

    def frame_line(self, frame, interface=''):
        now = frame.get('wall_time', time.time())
        prefix = '{}.{:03d} '.format(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
                                     int(now * 1000) % 1000)
        return prefix + '{} {} {} id=0x{:08X} dlc={} rtr={} err={} data=[{}] {} mono={}'.format(
            interface, frame['direction'], 'EFF' if frame['extended'] else 'SFF',
            frame['id'], len(frame['data']), frame['remote'], frame['error'],
            frame['data'].hex(' '), decode(frame['id'], frame['data'])
            if frame['extended'] and not frame['remote'] and not frame['error'] else '',
            frame.get('monotonic', 'unknown'))

    def batch_frames(self, frames, interface=''):
        if not self.frames_enabled or not frames:
            return
        text = '\n'.join(self.frame_line(f, interface) for f in frames)
        if not self.quiet_frames:
            debug(self.debug_enabled, text)
        if self.file:
            self.persist(text + '\n')

    def frame(self, frame, interface=''):
        self.batch_frames([frame], interface)

    def detail(self, message):
        if self.file:
            self.persist(message + '\n')

    def close(self):
        if self.file:
            file, self.file = self.file, None
            try:
                file.close()
            except OSError:
                pass


class SocketCan:
    def __init__(self, interface):
        self.interface = interface
        self.sock = None

    def __enter__(self):
        if not hasattr(socket, 'AF_CAN'):
            raise OSError('Linux SocketCAN is required')
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            self.sock.bind((self.interface,))
            self.sock.setblocking(False)
        except BaseException:
            self.sock.close()
            self.sock = None
            raise
        return self

    def __exit__(self, *args):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send(self, identifier, data):
        if not 0 <= identifier <= MASK or len(data) > 8:
            raise ValueError('invalid CAN ID or payload length')
        raw = FRAME.pack(identifier | EFF, len(data), data.ljust(8, b'\x00'))
        if self.sock.send(raw) != len(raw):
            raise OSError('short CAN write')

    def recv(self, timeout):
        if not select.select([self.sock], [], [], max(0, timeout))[0]:
            return None
        try:
            raw, _, flags, _ = self.sock.recvmsg(FRAME.size)
        except BlockingIOError:
            return None
        return unpack_frame(raw, 'TX' if flags & socket.MSG_DONTROUTE else 'RX')


def block_crc_input(number, data, config):
    if config.block_crc_order == 'data_only':
        if config.block_crc_number_padding:
            raise ValueError('data-only CRC cannot include number padding')
        return data
    if config.block_crc_number_padding:
        if not config.block_control_padding:
            raise ValueError('CRC number padding requires padded control frames')
        number = number.ljust(8, b'\x00')
    if config.block_crc_order == 'data_number':
        return data + number
    if config.block_crc_order == 'number_data':
        return number + data
    raise ValueError('unknown block CRC order: ' + config.block_crc_order)


def blocks(firmware, config):
    for offset in range(0, len(firmware), 128):
        actual = firmware[offset:offset + 128]
        padded = actual if config.tail_padding == 'none' else actual.ljust(
            128, bytes([int(config.tail_padding, 16)]))
        number = (offset // 128 + 1).to_bytes(2, config.block_number_endian)
        crc_data = padded if config.block_crc_scope == 'padded' else actual
        size = len(padded) if config.tail_size == 'padded' else len(actual)
        if config.block_size_includes_number:
            size += len(number)
        if config.block_size_field == 'zero':
            size = 0
        elif config.block_size_field != 'actual':
            raise ValueError('unknown block size field mode: ' + config.block_size_field)
        trailer = crc16(block_crc_input(number, crc_data, config)).to_bytes(2, config.block_crc_endian)
        trailer += size.to_bytes(2, config.field_endian)
        frames = [(0x4630, number)]
        frames.extend((0x4650 + (i // 8 if config.block_data_id_increment else 0),
                       padded[i:i + 8]) for i in range(0, len(padded), 8))
        frames.append((0x4670, trailer))
        yield offset // 128 + 1, len(actual), frames


def block_diagnostic(number, actual_length, frames, config):
    transmitted = b''.join(data for identifier, data in frames if 0x4650 <= identifier <= 0x465F)
    crc_data = transmitted if config.block_crc_scope == 'padded' else transmitted[:actual_length]
    crc_input = block_crc_input(frames[0][1], crc_data, config)
    return ('BLOCK_CHECK block={} actual={} transmitted={} number=[{}] '
            'crc_input_len={} crc_input=[{}] crc16=0x{:04X} trailer=[{}]').format(
                number, actual_length, len(transmitted), frames[0][1].hex(' '),
                len(crc_input), crc_input.hex(' '), crc16(crc_input), frames[-1][1].hex(' '))


def firmware_crc(firmware, config):
    data = firmware
    if config.firmware_crc_scope == 'padded' and config.tail_padding != 'none':
        data = data.ljust(((len(data) + 127) // 128) * 128, bytes([int(config.tail_padding, 16)]))
    return crc16(data).to_bytes(2, config.crc_endian)


def request_payload(identifier, data, config):
    """Apply captured control padding identically to transmission and preview."""
    if ((config.block_control_padding and identifier in (0x4630, 0x4670))
            or (config.firmware_size_padding and identifier == 0x4610)
            or (config.final_control_padding and identifier in (0x4690, 0x46B0, 0x46D0))):
        return data.ljust(8, b'\x00')
    return data


class BslbattFirmwareUpdater:
    def __init__(self, bus, config, log, clock=time.monotonic, sleep=time.sleep):
        self.bus, self.config, self.log = bus, config, log
        self.clock, self.sleep = clock, sleep
        self.events = None
        self.last_rx_time = None

    def record_frame(self, frame):
        if not self.log.frames_enabled:
            return
        frame = dict(frame, wall_time=time.time(), monotonic=self.clock())
        if self.events is None:
            self.log.frame(frame, self.config.can)
        else:
            if len(self.events) >= 4096:
                self.flush_frames()
                self.events = []
            self.events.append(frame)

    def flush_frames(self):
        events, self.events = self.events, None
        if events:
            self.log.batch_frames(events, self.config.can)

    def send(self, identifier, data):
        data = request_payload(identifier, data, self.config)
        self.bus.send(identifier, data)
        completed = self.clock()
        self.record_frame(dict(id=identifier, data=data, extended=True, remote=False,
                            error=False, direction='TX'))
        return completed

    def wait_ack(self, identifier, accepted, timeout=None, deadline=None):
        if deadline is None:
            deadline = self.clock() + (self.config.ack_timeout if timeout is None else timeout)
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError('timeout waiting for 0x{:04X}'.format(identifier))
            frame = self.bus.recv(remaining)
            if frame is None:
                continue
            self.last_rx_time = self.clock()
            self.record_frame(frame)
            if frame['error'] or frame['remote'] or not frame['extended'] or frame['id'] != identifier:
                continue
            try:
                data = response_payload(identifier, frame['data'])
            except ValueError:
                continue
            if data[0] not in VALID_CODES[identifier]:
                raise ProtocolError('unexpected status 0x{:02X} for 0x{:04X}'.format(data[0], identifier))
            if data[0] not in accepted:
                raise ProtocolError('BMS 0x{:02X}: {}'.format(data[0], CODES[data[0]]), data[0])
            return data

    def poll_status(self):
        self.log.write('RESTART_SETTLE seconds={}'.format(self.config.restart_settle_delay))
        self.sleep(self.config.restart_settle_delay)
        self.send(0x46D0, b'')
        self.log.write('completion=status_query_sent device_result=unconfirmed')
        xml_progress(100)
        xml_message('Update flow completed; device final status unconfirmed')
        return 'status_query_sent'

    def run(self, firmware):
        xml_message('Sending firmware size')
        xml_progress(0)
        # Bounded drain: preserve every observed frame in the log.
        deadline = self.clock() + 0.1
        while self.clock() < deadline:
            frame = self.bus.recv(0)
            if frame is None:
                break
            self.log.frame(frame, self.config.can)
        self.send(0x4610, len(firmware).to_bytes(4, self.config.firmware_size_endian))
        ack = self.wait_ack(0x4621, {0xA1})
        if int.from_bytes(ack[1:], self.config.response_size_endian) != 128:
            raise ProtocolError('only negotiated block size 128 is supported')
        self.log.write('SIZE_ACK_SETTLE seconds={}'.format(self.config.size_ack_delay))
        self.sleep(self.config.size_ack_delay)
        xml_message('Writing firmware')
        confirmed = 0
        previous_ack = None
        progress_at = self.clock()
        for number, length, frames in blocks(firmware, self.config):
            # Prepare payloads and CRC before entering the timed send path.
            if number == 1 or length < 128:
                self.log.write(block_diagnostic(number, length, frames, self.config))
            wire_frames = [(identifier, request_payload(identifier, data, self.config))
                           for identifier, data in frames]
            if previous_ack is not None:
                self.sleep(max(0, previous_ack + self.config.block_ack_delay - self.clock()))
            self.events = [] if self.log.frames_enabled else None
            starts, completed, submitted = [], None, 0
            ack_time, status = None, 'no_ack'
            try:
                for identifier, data in wire_frames:
                    if completed is not None:
                        self.sleep(max(0, completed + self.config.frame_interval - self.clock()))
                    starts.append(self.clock())
                    completed = self.send(identifier, data)
                    submitted += 1
                response = self.wait_ack(0x4681, {0xA2},
                                         deadline=completed + self.config.ack_timeout)
                ack_time = self.last_rx_time
                status = '0x{:02X}'.format(response[0])
            except (OSError, ValueError, ProtocolError, KeyboardInterrupt) as exc:
                status = '{}: {}'.format(type(exc).__name__, exc)
                self.flush_frames()
                self.log.write(block_diagnostic(number, length, frames, self.config))
                self.log.write('BLOCK_FAILED block={} last_confirmed={} {}'.format(number, number - 1, status))
                raise
            finally:
                self.flush_frames()
                intervals = [(b - a) * 1000 for a, b in zip(starts, starts[1:])]
                self.log.detail('BLOCK_TIMING block={} submitted={} application_timing=true '
                                'duration_ms={} gap_min_ms={} gap_median_ms={} gap_max_ms={} '
                                'ack_to_next_ms={} crc_to_ack_ms={} status={}'.format(
                    number, submitted, (completed - starts[0]) * 1000 if completed is not None else None,
                    min(intervals) if intervals else None, median(intervals) if intervals else None,
                    max(intervals) if intervals else None,
                    (starts[0] - previous_ack) * 1000 if previous_ack is not None else None,
                    (ack_time - completed) * 1000 if ack_time is not None else None, status))
            previous_ack = ack_time
            confirmed += length
            if self.clock() >= progress_at or confirmed == len(firmware):
                self.log.write('Confirmed block={} bytes={}/{} progress={}%'.format(
                    number, confirmed, len(firmware), confirmed * 90 // len(firmware)))
                xml_progress(confirmed * 90 // len(firmware))
                progress_at = self.clock() + 1
        xml_message('Verifying firmware')
        self.sleep(self.config.verify_delay)
        self.send(0x4690, firmware_crc(firmware, self.config))
        self.wait_ack(0x46A1, {0xA3})
        xml_progress(95)
        xml_message('Starting application')
        self.sleep(self.config.restart_delay)
        self.send(0x46B0, b'')
        self.wait_ack(0x46C1, {10, 11})
        return self.poll_status()


def update(args):
    """执行 --update：参数校验、固件读取、CAN 初始化、运行升级并映射退出码。
    Run --update: validate arguments, read firmware, initialize CAN, run the update, and map exit codes."""
    # Venus OS 上传后的固件路径应为绝对路径，避免脚本在不同工作目录下读错文件。
    # The firmware path uploaded by Venus OS should be absolute to avoid reading the wrong file from another cwd.
    if not os.path.isabs(args.file):
        xml_message("Firmware path error")
        debug(args.debug, "firmware file path must be absolute: {}".format(args.file))
        return EXIT_ARGUMENT_ERROR
    if not os.path.isfile(args.file):
        xml_message("Firmware path error")
        debug(args.debug, "firmware file does not exist or is not a file: {}".format(args.file))
        return EXIT_FILE_ERROR

    try:
        can_interface, node_id = resolve_update_target(args)
    except DeviceIdError as exc:
        xml_message("Device id error")
        debug(args.debug, str(exc))
        return EXIT_ARGUMENT_ERROR
    except ValueError as exc:
        xml_message("Invalid arguments")
        debug(args.debug, str(exc))
        return EXIT_ARGUMENT_ERROR

    try:
        # 先在本地读取并检查固件，避免已经让设备进入升级模式后才发现文件问题。
        # Read and check the firmware locally first so file issues are found before the device enters update mode.
        firmware = read_firmware(args.file)
        validate_bslbatt_firmware(firmware)
    except OSError as exc:
        xml_message("Firmware file error")
        debug(args.debug, str(exc))
        return EXIT_FILE_ERROR
    except FirmwareError as exc:
        xml_message("Firmware is not compatible")
        debug(args.debug, str(exc))
        return EXIT_FIRMWARE_ERROR

    log = Logger(args.can_log, args.debug)
    bus = SocketCan(can_interface)
    try:
        try:
            bus.__enter__()
        except OSError as exc:
            xml_message('CAN init failed')
            log.write(str(exc))
            return EXIT_CAN_INIT_ERROR
        config = SimpleNamespace(can=can_interface, **UPGRADE_CONFIG)
        BslbattFirmwareUpdater(bus, config, log).run(firmware)
        return EXIT_OK
    except TimeoutError as exc:
        xml_message('Device response timeout')
        log.write(str(exc))
        return EXIT_TIMEOUT
    except ProtocolError as exc:
        log.write(str(exc))
        print('device error: {}'.format(exc), file=sys.stderr, flush=True)
        if exc.code in (2, 8):
            xml_message('Verification failed')
            return EXIT_VERIFY_FAILED
        if exc.code in (4, 6, 20):
            xml_message('Device memory error')
            return EXIT_MEMORY_ERROR
        if exc.code in (1, 5, 7, 9, 18):
            xml_message('Firmware error')
            return EXIT_FIRMWARE_ERROR
        xml_message('Update failed')
        return EXIT_GENERAL_ERROR
    except OSError as exc:
        xml_message('CAN communication failed')
        log.write(str(exc))
        return EXIT_CAN_COMM_ERROR
    except KeyboardInterrupt:
        xml_message('Update interrupted; device final status unknown')
        return 130
    except Exception as exc:
        xml_message('Update failed')
        log.write(repr(exc))
        return EXIT_GENERAL_ERROR
    finally:
        try:
            bus.__exit__()
        finally:
            log.close()


def build_parser():
    """定义合并工具命令行参数；兼容 Venus OS 文档示例和显式模式。
    Define merged-tool CLI arguments, compatible with Venus OS document examples and explicit modes."""
    parser = argparse.ArgumentParser(description="List and update BSLBATT devices for Venus OS")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-l", "--list", action="store_true", help="list updatable devices")
    mode.add_argument("-u", "--update", action="store_true", help="update firmware")

    parser.add_argument(
        "-c",
        "--can",
        help=(
            "CAN interface. For --list this selects which bus to scan; omitted means scan all can*/vecan*. "
            "For --update this is the bus selected by Venus OS, for example can0 or vecan0"
        ),
    )
    parser.add_argument(
        "-n",
        "--node-id",
        help="CAN connection-id returned from list output; BSLBATT single-device tools must use 0x0",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="passive discovery timeout in seconds for --list")

    parser.add_argument(
        "--connection",
        help="legacy socketcan connection string, for example socketcan:can0/0x0; prefer -c/-n for VRM",
    )
    parser.add_argument("-f", "--file", help="firmware file absolute path for --update")
    parser.add_argument("-d", "--debug", action="store_true", help="write debug logs to stderr")
    parser.add_argument(
        "--can-log",
        default="venus_firmware_update_can.log",
        help="local file for parsed CAN TX/RX logs during --update; use an empty value to disable",
    )
    return parser


def infer_mode(args):
    """
    Infer the Venus OS remote-toolbox operation when no explicit mode is used.

    The Victron document examples call CAN list tools as `tool -c can1` and
    update tools as `tool -c can1 -f file -n id`, so `-f` is the update signal.
    """
    if args.list:
        return "list"
    if args.update:
        return "update"
    if args.file:
        return "update"
    return "list"


def main():
    """脚本入口：配置输出缓冲、解析参数，然后执行列表或升级。
    Script entry point: configure output buffering, parse arguments, then list or update."""
    configure_stdout()
    args = build_parser().parse_args()
    mode = infer_mode(args)

    if mode == "list":
        if args.timeout <= 0:
            debug(args.debug, "timeout must be greater than zero")
            return EXIT_ARGUMENT_ERROR
        return list_devices(args)

    if mode == "update":
        if not args.file:
            xml_message("Invalid arguments")
            debug(args.debug, "--update requires --file")
            return EXIT_ARGUMENT_ERROR
        return update(args)

    # infer_mode() should always resolve to list or update.
    return EXIT_ARGUMENT_ERROR


if __name__ == "__main__":
    # shell/系统通常只使用退出码低 8 位，这里显式截断。
    # Shells/systems usually use only the low 8 bits of the exit code, so truncate explicitly here.
    sys.exit(main() & 0xFF)
