#!/usr/bin/env python3
"""
BSLBATT Venus OS firmware update tool.

Called by Victron GX / Venus OS after a user uploads firmware through VRM.
Stdout must contain XML progress/messages only. Debug output goes to stderr.

中文阅读提示：
    这个脚本是 Venus OS 调用的固件升级工具。标准输出 stdout 只能打印
    Venus OS 能识别的 XML 进度/消息；调试日志必须走 stderr，避免污染 XML。
    主流程在 update() 和 BslbattFirmwareUpdater.run() 中。

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


# Venus OS / 调用方通过退出码判断失败类型；0 表示成功。
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

# Linux SocketCAN 原始 CAN 帧结构：
#   can_id: 4 字节
#   can_dlc: 1 字节
#   padding: 3 字节
#   data: 8 字节
CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)

# SocketCAN 在 can_id 高位中携带扩展帧、远程帧、错误帧标志。
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_ID_MASK = 0x1FFFFFFF

# BSLBATT BMS 固件升级协议使用的 29 位扩展帧 ID。
# 命名规则：
#   REQ 表示上位机发给 BMS 的请求；
#   ACK 表示 BMS 回复上位机的确认；
#   DATA_FRAME_BASE_ID 是数据帧起始 ID，每发一帧递增 1。
BMS_UPGRADE_START_REQ_ID = 0x18A055AA
BMS_UPGRADE_START_ACK_ID = 0x18A0AA55
BMS_UPGRADE_DATA_FRAME_BASE_ID = 0x13000001
BMS_UPGRADE_DATA_ACK_ID = 0x18A1AA55
BMS_UPGRADE_FINISH_REQ_ID = 0x18A255AA
BMS_UPGRADE_FINISH_ACK_ID = 0x18A2AA55
BMS_UPGRADE_ERROR_ID = 0x18A3AA55

# 每个 CAN 数据帧最多 8 字节。这里协议定义前 7 字节是固件数据，
# 第 8 字节是前 7 字节求和后的低 8 位校验值。
BMS_UPGRADE_FRAME_DATA_SIZE = 7
BMS_UPGRADE_FRAME_TOTAL_SIZE = 8
# oldcode uses APP_BMS_UPGRADE_ACK_BATCH_SIZE; BMS_CAN.xlsx states the BMS
# acknowledges once after every 10 firmware data frames.
BMS_UPGRADE_ACK_BATCH_SIZE = 10

# 各阶段等待 BMS ACK 的超时时间。数据帧之间保留很短间隔，避免总线过载。
BMS_UPGRADE_START_ACK_TIMEOUT_SECONDS = 5.0
BMS_UPGRADE_DATA_ACK_TIMEOUT_SECONDS = 10.0
BMS_UPGRADE_FINISH_ACK_TIMEOUT_SECONDS = 10.0
BMS_UPGRADE_FRAME_INTERVAL_SECONDS = 0.2


class FirmwareError(Exception):
    """固件包内容或格式不符合升级协议。"""

    pass


class DeviceNotFoundError(Exception):
    """预留异常：表示未找到目标设备。当前 locate_device() 只清空缓冲区。"""

    pass


class VerifyFailedError(Exception):
    """预留异常：表示升级后固件校验失败。当前 verify_firmware() 尚未实现校验。"""

    pass


class VerifyTimeoutError(Exception):
    """预留异常：表示等待校验结果超时。"""

    pass


class MemoryErrorOnDevice(Exception):
    """BMS 通过错误帧报告升级或存储异常。"""

    pass


def configure_stdout():
    """配置 stdout/stderr 为行缓冲，确保 Venus OS 能及时收到 XML 进度。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True, write_through=True)


def debug(enabled, message):
    """调试日志只写 stderr，避免破坏 stdout 上的 XML 协议。"""
    if enabled:
        print(message, file=sys.stderr, flush=True)


def xml_escape(value):
    """对 XML 文本内容做转义，避免消息里出现 <、& 等字符导致 XML 非法。"""
    return html.escape(str(value), quote=False)


def xml_message(text):
    """向 Venus OS 输出普通消息。stdout 上只能出现这类 XML。"""
    print('<message type="normal">{}</message>'.format(xml_escape(text)), flush=True)


_last_progress_level = None


def xml_progress(level):
    """向 Venus OS 输出进度；相同进度不会重复打印，减少 stdout 噪声。"""
    global _last_progress_level
    level = max(0, min(100, int(level)))
    if level == _last_progress_level:
        return
    _last_progress_level = level
    print('<progress level="{}" />'.format(level), flush=True)


def parse_connection(connection):
    """解析 Venus OS 传入的连接字符串，例如 socketcan:can0/0x2A。"""
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
    """打开 Linux SocketCAN RAW 套接字并绑定到 can0/vecan0 等接口。"""
    if not hasattr(socket, "AF_CAN") or not hasattr(socket, "CAN_RAW"):
        raise OSError("SocketCAN is required on Victron GX / Venus OS")
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((interface_name,))
    sock.settimeout(2.0)
    return sock


def pack_can_frame(can_id, payload, extended=False):
    """把 CAN ID 和 payload 打包成 Linux SocketCAN 需要的二进制帧。"""
    payload = bytes(bytearray(payload))
    if len(payload) > 8:
        raise ValueError("CAN payload must be <= 8 bytes")
    frame_id = can_id & CAN_ID_MASK
    if extended:
        frame_id |= CAN_EFF_FLAG
    return struct.pack(CAN_FRAME_FORMAT, frame_id, len(payload), payload.ljust(8, b"\x00"))


def send_can(sock, can_id, payload, extended=False):
    """发送一帧 CAN。extended=True 时使用 29 位扩展帧。"""
    sock.send(pack_can_frame(can_id, payload, extended))


def unpack_can_frame(raw_frame):
    """把 SocketCAN 收到的原始帧拆成便于日志和协议判断的字典。"""
    can_id, data_len, data = struct.unpack(CAN_FRAME_FORMAT, raw_frame)
    return {
        "can_id": can_id & CAN_ID_MASK,
        "is_extended": bool(can_id & CAN_EFF_FLAG),
        "is_remote": bool(can_id & CAN_RTR_FLAG),
        "is_error": bool(can_id & CAN_ERR_FLAG),
        "data": data[:data_len],
    }


def read_firmware_from_zip(path):
    """从 zip 固件包中读取唯一的 .bin/.fw/.img 文件，并先做 zip CRC 检查。"""
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
    """读取固件文件；支持直接读取二进制文件，也支持读取 zip 包中的固件。"""
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
    """计算固件 CRC32，仅用于日志/展示，不参与当前协议校验。"""
    return "{:08X}".format(binascii.crc32(data) & 0xFFFFFFFF)


def le32(value):
    """把整数编码为小端 32 位，协议中的 size/frame_count 都按小端传输。"""
    return struct.pack("<I", value & 0xFFFFFFFF)


def read_le32(data):
    """从 payload 前 4 字节读取小端 32 位整数；长度不足时按 0 处理。"""
    if len(data) < 4:
        return 0
    return struct.unpack("<I", data[:4])[0]


def payload_hex(payload):
    """把字节序列格式化成十六进制字符串，用于调试日志。"""
    return " ".join("{:02X}".format(byte) for byte in payload)


def can_id_name(can_id):
    """把关键 CAN ID 转成可读名称，方便分析 CAN 日志。"""
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
    """按控制帧类型解析 payload，主要用于 CAN 日志辅助排查。"""
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
    """向上取整除法，用于把固件长度补齐到 7 字节帧边界。"""
    return (value + divisor - 1) // divisor


def validate_bslbatt_firmware(firmware):
    """
    Validate BSLBATT firmware format before touching the device.

    中文说明：
        当前实现只做最基础检查：非空、传输大小/帧数不超过 32 位。
        真正的固件头、型号、版本、签名、CRC 等校验还需要后续按实际包格式补齐。

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

    中文说明：
        这个类只关心 BMS CAN 升级协议本身：
        1. 发送开始升级请求；
        2. 按 7 字节一帧发送固件数据；
        3. 每 10 帧等待一次 BMS 数据 ACK；
        4. 发送结束升级请求；
        5. 解析错误帧、记录 CAN 日志。
    """

    def __init__(self, sock, node_id, firmware, firmware_info, debug_enabled=False, can_log_path=None):
        """保存升级上下文和传输状态；初始化时写一条 CAN 日志头。"""
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
        """类内部调试日志入口，受 --debug 控制。"""
        debug(self.debug_enabled, message)

    def send_extended(self, can_id, payload):
        """发送 BSLBATT 升级协议使用的 29 位扩展 CAN 帧，并打印调试日志。"""
        payload_hex = " ".join("{:02X}".format(byte) for byte in payload)
        self.debug("TX id=0x{:08X} len={} data={}".format(can_id, len(payload), payload_hex))
        send_can(self.sock, can_id, payload, extended=True)

    def drain_control_frames(self):
        """清空当前 socket 中已积压的 CAN 帧，避免旧 ACK 干扰新一次升级流程。"""
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
        """等待指定 CAN ID 的 ACK；期间会忽略无关帧，遇到错误帧立即失败。"""
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
            # 升级协议只接受扩展数据帧；错误帧、远程帧、标准帧都忽略。
            if frame["is_error"] or frame["is_remote"] or not frame["is_extended"]:
                continue

            can_id = frame["can_id"]
            payload = frame["data"]
            # BMS 主动发错误帧时，映射成设备端内存/升级异常。
            if can_id == BMS_UPGRADE_ERROR_ID:
                self.log_rx(can_id, payload)
                payload_str = payload_hex(payload) if payload else "(empty)"
                raise MemoryErrorOnDevice(
                    "BMS reported upgrade error frame: id=0x{:08X} len={} data=[{}] expected_ack=0x{:08X} sent_frames={}/{} next_offset={}".format(
                        can_id,
                        len(payload),
                        payload_str,
                        expected_can_id,
                        self.sent_frame_count,
                        self.frame_count,
                        self.next_offset,
                    )
                )

            # 总线上可能有其他设备/其他协议的帧，这里只等待目标 ACK。
            if can_id != expected_can_id:
                continue

            self.log_rx(can_id, payload)
            # 控制帧固定 8 字节，不满足则继续等下一帧。
            if len(payload) != BMS_UPGRADE_FRAME_TOTAL_SIZE:
                self.debug("Ignore invalid ACK length for 0x{:08X}: {}".format(can_id, len(payload)))
                continue
            return payload

    def log_rx(self, can_id, payload):
        """把收到的有效控制帧输出到调试日志。"""
        self.debug("RX id=0x{:08X} len={} data={}".format(can_id, len(payload), payload_hex(payload)))

    def write_can_log(self, line):
        """追加写 CAN 日志；写失败后会关闭后续日志写入，避免影响升级。"""
        if not self.can_log_path or self.can_log_failed:
            return
        try:
            with open(self.can_log_path, "a") as log_file:
                log_file.write("{:.3f} {}\n".format(time.time(), line))
        except OSError as exc:
            self.can_log_failed = True
            self.debug("CAN log write failed: {}".format(exc))

    def log_can_frame(self, direction, frame, expected_can_id=None):
        """把 CAN 帧解析成一行可读日志，包含方向、ID、标志位、payload 和解析字段。"""
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
        """开始/结束请求的 payload：补齐后的传输字节数 + 总帧数。"""
        return le32(self.transfer_size) + le32(self.frame_count)

    def locate_device(self):
        """设备定位阶段。当前协议没有主动探测，只清空旧帧作为准备。"""
        self.drain_control_frames()

    def enter_bootloader(self):
        """发送开始升级请求，等待 BMS 的 START_ACK，表示设备进入升级流程。"""
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
        """擦除阶段占位。当前 BMS 协议可能在 START_REQ 后由设备内部自动处理。"""
        return

    def write_firmware(self):
        """按批次发送固件数据；每 10 帧等待一次 DATA_ACK。"""
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
        """发送单个数据帧：7 字节固件数据 + 1 字节求和校验。"""
        chunk = self.firmware[self.next_offset : self.next_offset + BMS_UPGRADE_FRAME_DATA_SIZE]
        read_size = len(chunk)
        # 最后一帧不足 7 字节时用 0xFF 填充，保证 payload 固定 8 字节。
        payload = bytearray(b"\xFF" * BMS_UPGRADE_FRAME_TOTAL_SIZE)
        payload[:read_size] = chunk
        # 第 8 字节是前 7 字节求和后的低 8 位，用于 BMS 侧快速校验。
        payload[7] = sum(payload[:BMS_UPGRADE_FRAME_DATA_SIZE]) & 0xFF

        # 数据帧 CAN ID 从 0x13000001 开始，每发送一帧递增。
        can_id = BMS_UPGRADE_DATA_FRAME_BASE_ID + self.sent_frame_count
        self.send_extended(can_id, payload)

        self.sent_frame_count += 1
        self.next_offset += read_size
        self.last_frame_tail = bytes(payload[4:8])

    def wait_data_ack(self, expected_frame_count):
        """等待数据 ACK，并校验 ACK 中的累计帧数和最后一帧尾部字段。"""
        while True:
            payload = self.receive_control_frame(BMS_UPGRADE_DATA_ACK_ID, BMS_UPGRADE_DATA_ACK_TIMEOUT_SECONDS)
            acked_frame_count = read_le32(payload)
            tail = payload[4:8]
            # 全 FF ACK 被视为通用确认，直接通过。
            if payload == b"\xFF" * BMS_UPGRADE_FRAME_TOTAL_SIZE:
                return
            # ACK 中应返回当前批次已接收的累计帧数，不匹配则忽略继续等。
            if acked_frame_count != expected_frame_count:
                self.debug(
                    "Ignore data ACK frameCount={}, expected={}".format(acked_frame_count, expected_frame_count)
                )
                continue
            # ACK 后 4 字节应等于最后一帧 payload[4:8]，用于确认 BMS 收到正确批次。
            if tail != self.last_frame_tail:
                self.debug(
                    "Ignore data ACK tail={}, expected={}".format(tail.hex().upper(), self.last_frame_tail.hex().upper())
                )
                continue
            return

    def log_transfer_progress(self, stage):
        """写入传输进度到 CAN 日志；SEND 阶段同百分比只记录一次。"""
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
        """校验阶段占位。当前没有额外读取设备校验结果。"""
        return

    def reboot_application(self):
        """发送结束升级请求，等待 FINISH_ACK，BMS 随后应启动新应用。"""
        self.send_extended(BMS_UPGRADE_FINISH_REQ_ID, self.size_and_count_payload())
        self.receive_control_frame(BMS_UPGRADE_FINISH_ACK_ID, BMS_UPGRADE_FINISH_ACK_TIMEOUT_SECONDS)

    def run(self):
        """对外的完整升级流程，同时输出 Venus OS 需要的 XML 消息和进度。"""
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
    """执行 --update：参数校验、固件读取、CAN 初始化、运行升级并映射退出码。"""
    try:
        # -s/--connection 来自 Venus OS 列表 XML，格式必须是 socketcan:接口/节点ID。
        can_interface, node_id = parse_connection(args.connection)
    except ValueError as exc:
        xml_message("Invalid arguments")
        debug(args.debug, str(exc))
        return EXIT_ARGUMENT_ERROR

    # Venus OS 上传后的固件路径应为绝对路径，避免脚本在不同工作目录下读错文件。
    if not os.path.isabs(args.file):
        xml_message("Invalid arguments")
        debug(args.debug, "firmware file path must be absolute: {}".format(args.file))
        return EXIT_ARGUMENT_ERROR

    try:
        # 先在本地读取并检查固件，避免已经让设备进入升级模式后才发现文件问题。
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
        # 只有文件和参数都通过后才打开 CAN；CAN 初始化失败单独返回退出码 2。
        sock = open_can(can_interface)
    except OSError as exc:
        xml_message("CAN init failed")
        debug(args.debug, str(exc))
        return EXIT_CAN_INIT_ERROR

    updater = BslbattFirmwareUpdater(sock, node_id, firmware, firmware_info, args.debug, args.can_log)
    try:
        updater.run()
        return EXIT_OK
    # 下面的异常处理会把内部错误转换成 Venus OS 可识别的 XML 消息和退出码。
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
        # 设备返回的错误帧详情对排查非常关键，无论是否开启 --debug 都打到 stderr。
        print("device error: {}".format(exc), file=sys.stderr, flush=True)
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
        # 无论成功失败都关闭 CAN socket，释放接口资源。
        sock.close()


def build_parser():
    """定义命令行参数；Venus OS 调用时必须带 --update、-s 和 -f。"""
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
    """脚本入口：配置输出缓冲、解析参数，然后执行升级。"""
    configure_stdout()
    args = build_parser().parse_args()
    if not args.update:
        # 当前脚本只实现升级动作；没有 --update 时认为参数错误。
        return EXIT_ARGUMENT_ERROR
    return update(args)


if __name__ == "__main__":
    # shell/系统通常只使用退出码低 8 位，这里显式截断。
    sys.exit(main() & 0xFF)
