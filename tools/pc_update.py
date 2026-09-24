#!/usr/bin/env python3
"""Standalone experimental CAN updater. Importing this module does not open CAN."""
import argparse
import json
import math
import select
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET

FIRMWARE_CHOICES = {
    0: '/opt/victronenergy/mqtt-rpc/thirdparty/bslbatt/P41288V110-41289-1.51T-000.bin',
    1: '/opt/victronenergy/mqtt-rpc/thirdparty/bslbatt/P41288V110-41289-1.52T-000.bin',
}

# 固定调试配置：无参数运行即升级同目录的 1.52T 固件。
# 默认值依据 logs/BMS Upgrade All.txt（CAN Update V2.7）。
# 2179包均获A2，整体CRC获A3；重启获0B，但最终状态为0F（升级失败）。
# 日志未标注帧类型和DLC；按其8字节载荷补零，扩展帧沿用此前实测配置。
# 此抓包确认传输与校验流程，不代表设备最终更新成功。
FIXED_CONFIG = dict(
    debug_attempt='CAN Update V2.7 capture: data-only CRC, zero size field, padded controls',
    block_size_includes_number=False,
    block_size_field='zero',       # 抓包4670后六字节均为0；actual可恢复原长度字段计算
    block_data_id_increment=False,
    block_crc_number_padding=False,
    block_control_padding=True,    # 4630/4670补零为8字节
    firmware_size_padding=True,    # 4610：4字节小端长度 + 4字节0
    final_control_padding=True,    # 4690/46B0/46D0均补零至8字节
    can='can0',
    file=str(Path(__file__).resolve().parent / 'P41288V110-41289-1.52T-000.bin'),
    log='/opt/victronenergy/mqtt-rpc/thirdparty/bslbatt/debug.log',
    discovery_tool=str(Path(__file__).resolve().with_name('bslbatt-tool.py')),
    discovery_timeout=5.0,
    firmware_size_endian='little',  # 实测 0x4610 小端大小获得 A1
    response_size_endian='big',     # 实测 A1 00 80 表示 128 字节
    block_number_endian='little', # 抓包：01 00、02 00……83 08（2179）
    field_endian='little',         # 默认大小字段为0，抓包不能确定其字节序
    block_crc_order='data_only',  # 抓包CRC仅含128字节数据，不含4630包号或补零
    block_crc_endian='little',    # 首包CRC=A200，线上00 A2；与文档130字节定义不同
    crc_endian='little',          # 整体CRC=FA8D，线上8D FA，获A3 01 00
    tail_padding='ff',            # 尾包36字节数据 + 92字节FF，仍发送16帧
    tail_size='actual',
    block_crc_scope='padded',     # 尾包含FF的128字节CRC=192B，线上2B 19
    firmware_crc_scope='actual',  # 整体CRC仅计算278820字节，不含尾包补齐
    stop_after='complete',
    completion_criterion='status_query_sent', # 用户判据：首次发送46D0即结束；device_status等待0D
    frame_interval=0.003,       # 包内目标帧间隔3ms；实际发送还受系统调度影响
    size_ack_delay=0.048,       # 抓包A1到首个包号约48ms
    block_ack_delay=0.075,      # A2后等待75ms再发下一包，验证设备处理时间是否影响包号错误
    verify_delay=0.032,         # 最后一个A2到4690约31ms
    restart_delay=0.032,        # A3到46B0约32ms
    restart_settle_delay=15.0,  # 0B后约15秒开始查询状态
    ack_timeout=30.0,
    status_interval=3.0,       # 查询发送间隔；无响应时也每约3秒重新查询
    update_timeout=300.0,
)

FRAME = struct.Struct('=IB3x8s')
EFF, RTR, ERR, MASK = 0x80000000, 0x40000000, 0x20000000, 0x1FFFFFFF
REQUESTS = {0x4610: 'SIZE', 0x4630: 'BLOCK_NUMBER', 0x4650: 'BLOCK_DATA',
            0x4670: 'BLOCK_CRC_SIZE', 0x4690: 'FIRMWARE_CRC',
            0x46B0: 'RESTART', 0x46D0: 'STATUS_QUERY'}
RESPONSES = {0x4621: 'SIZE_ACK', 0x4681: 'BLOCK_ACK', 0x46A1: 'CRC_ACK',
             0x46C1: 'RESTART_ACK', 0x46E1: 'STATUS_ACK'}
CODES = {0xA1: '大小通过', 0xA2: '分包通过', 0xA3: '固件校验通过',
         1: '固件大小错误', 2: '分包 CRC 不匹配', 3: '分包序号错误',
         4: '分包数据写入错误', 5: '分包大小错误', 6: 'CRC 写入错误',
         7: '固件总大小异常', 8: '固件 CRC 不匹配', 9: '无效固件',
         10: '转发中', 11: '本机开始升级', 12: '转发中', 13: '升级成功',
         14: '转发错误', 15: '升级失败', 16: '本机升级中',
         17: '暂不符合升级条件', 18: '设备版本不符合要求',
         19: '命令不符合流程', 20: '固件 CRC16 存储故障', 21: '暂不符合升级条件'}
VALID_CODES = {0x4621: {0xA1, 1}, 0x4681: {0xA2, 2, 3, 4, 5, 19},
               0x46A1: {0xA3, 6, 7, 8, 19, 20},
               0x46C1: {9, 10, 11, 19, 21}, 0x46E1: set(range(12, 20))}
CONFIG_FIELDS = ('field_endian', 'crc_endian', 'tail_padding', 'tail_size',
                 'block_crc_scope', 'firmware_crc_scope')


class ProtocolError(Exception):
    pass


class DeviceOffline(ProtocolError):
    pass


def can_diagnostic(config, log, stage):
    """Read interface statistics only; never change CAN configuration."""
    try:
        result = subprocess.run(['ip', '-details', '-statistics', 'link', 'show',
                                 'dev', config.can], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=3)
        log.write('CAN_DIAGNOSTIC {} exit={} stdout={} stderr={}'.format(
            stage, result.returncode, result.stdout.strip(), result.stderr.strip()))
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.write('CAN_DIAGNOSTIC {} unavailable: {}'.format(stage, exc))


def check_device_online(config, log, stage):
    """Return matching devices; an empty discovery result does not block updates."""
    log.write('ONLINE_CHECK {} start interface={}'.format(stage, config.can))
    try:
        result = subprocess.run(
            [sys.executable, config.discovery_tool, '--list', '-c', config.can,
             '--timeout', str(config.discovery_timeout), '--debug'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', timeout=config.discovery_timeout + 10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolError('ONLINE_CHECK {} 检测执行失败，停止升级：{}'.format(stage, exc)) from exc
    for line in result.stderr.splitlines():
        log.write('DISCOVERY {} {}'.format(stage, line))
    for line in result.stdout.splitlines():
        log.write('DISCOVERY_XML {} {}'.format(stage, line))
    if result.returncode:
        raise ProtocolError('ONLINE_CHECK {} 检测工具错误 exit={}，停止升级；请检查 CAN/脚本'.format(
            stage, result.returncode))
    try:
        root = ET.fromstring('<devices>' + result.stdout + '</devices>')
    except ET.ParseError as exc:
        raise ProtocolError('ONLINE_CHECK {} 无效设备 XML，停止升级'.format(stage)) from exc
    devices = [device for device in root.findall('device')
               if device.get('type') == 'bslbatt'
               and device.get('connection') == 'socketcan:{}/0x0'.format(config.can)]
    if not devices:
        return devices
    log.write('ONLINE_CHECK {} ONLINE count={} serial={} version={}'.format(
        stage, len(devices), devices[0].get('serial', ''), devices[0].get('version', '')))
    return devices


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
    # STATUS_ACK also carries an observed second byte (e.g. 0D 01).
    # Preserve it as extra data without assigning undocumented semantics.
    # Continue accepting legacy one-byte status responses.
    if identifier == 0x46E1 and len(data) in (2, 8):
        expected = 2
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
            name, data[0], CODES.get(data[0], '未知状态'),
            ' extra=' + data[1:].hex(' ') if len(data) > 1 else '')
    sizes = {0x4610: 4, 0x4630: 2, 0x4670: 4, 0x4690: 2, 0x46B0: 0, 0x46D0: 0}
    if identifier in sizes and len(data) != sizes[identifier]:
        if len(data) == 8 and not any(data[sizes[identifier]:]):
            return name + ' zero_padding={}'.format(8 - sizes[identifier])
        return '{} invalid_length={} expected={}'.format(name, len(data), sizes[identifier])
    return name


class Logger:
    def __init__(self, path=None, quiet_frames=False):
        self.file = open(path, 'a', encoding='utf-8') if path else None
        self.quiet_frames = quiet_frames

    def write(self, message):
        now = time.time()
        line = '{}.{:03d} {}'.format(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
                                    int(now * 1000) % 1000, message)
        print(line, flush=True)
        if self.file:
            self.file.write(line + '\n')
            self.file.flush()

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
        if not frames:
            return
        text = '\n'.join(self.frame_line(f, interface) for f in frames)
        if not self.quiet_frames:
            print(text, flush=True)
        if self.file:
            self.file.write(text + '\n')
            self.file.flush()

    def frame(self, frame, interface=''):
        self.batch_frames([frame], interface)

    def detail(self, message):
        if self.file:
            self.file.write(message + '\n')
            self.file.flush()

    def close(self):
        if self.file:
            self.file.close()


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
            raise
        return self

    def __exit__(self, *args):
        self.sock.close()

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


class Updater:
    def __init__(self, bus, config, log, clock=time.monotonic, sleep=time.sleep):
        self.bus, self.config, self.log = bus, config, log
        self.clock, self.sleep = clock, sleep
        self.events = None
        self.last_rx_time = None

    def record_frame(self, frame):
        frame = dict(frame, wall_time=time.time(), monotonic=self.clock())
        if self.events is None:
            self.log.frame(frame, self.config.can)
        else:
            if len(self.events) >= 4096:
                raise ProtocolError('block event buffer overflow')
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
                raise ProtocolError('BMS 0x{:02X}: {}'.format(data[0], CODES[data[0]]))
            return data

    def status(self):
        self.send(0x46D0, b'')
        return self.wait_ack(0x46E1, {12, 13, 16})[0]

    def poll_status(self):
        deadline = self.clock() + self.config.update_timeout
        self.log.write('RESTART_SETTLE seconds={}'.format(self.config.restart_settle_delay))
        self.sleep(min(self.config.restart_settle_delay, self.config.update_timeout))
        while self.clock() < deadline:
            next_query = min(self.clock() + self.config.status_interval, deadline)
            self.send(0x46D0, b'')
            if self.config.completion_criterion == 'status_query_sent':
                self.log.write('100% 流程成功：按46D0已发送判据完成；设备最终结果未确认 '
                               'completion=status_query_sent device_result=unconfirmed')
                return 'status_query_sent'
            try:
                result = self.wait_ack(0x46E1, {12, 13, 16},
                                       min(self.config.ack_timeout, next_query - self.clock()))
                if result[0] == 13:
                    self.log.write('100% Update successful')
                    return
            except TimeoutError:
                self.log.write('No status response; waiting within update deadline')
            self.sleep(max(0, next_query - self.clock()))
        raise TimeoutError('overall update status timeout')

    def stop(self, stage):
        if self.config.stop_after == stage:
            self.log.write('{} 阶段完成，尚未完成升级'.format(stage))
            return True
        return False

    def run(self, firmware):
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
        if self.stop('size'):
            return
        self.log.write('SIZE_ACK_SETTLE seconds={}'.format(self.config.size_ack_delay))
        self.sleep(self.config.size_ack_delay)
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
            self.events = []
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
                progress_at = self.clock() + 1
        if self.stop('transfer'):
            return
        self.sleep(self.config.verify_delay)
        self.send(0x4690, firmware_crc(firmware, self.config))
        self.wait_ack(0x46A1, {0xA3})
        if self.stop('verify'):
            return
        self.sleep(self.config.restart_delay)
        self.send(0x46B0, b'')
        self.wait_ack(0x46C1, {10, 11})
        return self.poll_status()


def preview(firmware, config, log):
    def show(identifier, data):
        data = request_payload(identifier, data, config)
        log.write('PLAN id=0x{:04X} dlc={} data=[{}]'.format(identifier, len(data), data.hex(' ')))
    show(0x4610, len(firmware).to_bytes(4, config.firmware_size_endian))
    log.write('PLAN wait 0x4621 A1 + negotiated block size 128')
    if config.stop_after == 'size':
        return
    for number, _, frames in blocks(firmware, config):
        log.write('PLAN block={}'.format(number))
        for identifier, data in frames:
            show(identifier, data)
        log.write('PLAN wait 0x4681 A2')
    if config.stop_after == 'transfer':
        return
    show(0x4690, firmware_crc(firmware, config))
    log.write('PLAN wait 0x46A1 A3 + two uninterpreted bytes')
    if config.stop_after != 'verify':
        show(0x46B0, b'')
        log.write('PLAN wait 0x46C1 0A/0B')
        show(0x46D0, b'')
        log.write('PLAN finish after first status query; device result unconfirmed'
                  if config.completion_criterion == 'status_query_sent' else
                  'PLAN repeat status query until 0D or failure/timeout')


def parser():
    p = argparse.ArgumentParser(
        description='无参数使用默认1.52T固件；-b 0选择1.51T，-b 1选择1.52T的固定远程路径。')
    p.set_defaults(**FIXED_CONFIG)
    p.add_argument('-b', type=int, choices=(0, 1), default=None,
                   help='固件选择：0=1.51T，1=1.52T；不传时沿用默认1.52T配置')
    p.add_argument('--check-online', action='store_true', help='仅调用 bslbatt-tool.py 检查在线，不发送升级命令')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.b is not None:
        args.file = FIRMWARE_CHOICES[args.b]
    if args.completion_criterion not in ('status_query_sent', 'device_status'):
        p.error('invalid completion_criterion')
    for name in ('frame_interval', 'size_ack_delay', 'block_ack_delay', 'verify_delay',
                 'restart_delay', 'restart_settle_delay', 'ack_timeout', 'status_interval',
                 'update_timeout', 'discovery_timeout'):
        value = getattr(args, name)
        nonnegative = name in ('frame_interval', 'size_ack_delay', 'block_ack_delay',
                               'verify_delay', 'restart_delay', 'restart_settle_delay')
        if not math.isfinite(value) or value < 0 or (not nonnegative and value == 0):
            p.error('{} must be finite and {}'.format(name, 'nonnegative' if nonnegative else 'positive'))
    log = None
    try:
        log = Logger(args.log, quiet_frames=True)
        log.write('CONFIG ' + json.dumps(vars(args), sort_keys=True))
        if args.check_online:
            check_device_online(args, log, 'check-only')
            return 0
        log.write('Experimental: N=1, extended IDs; data ID increment={}; CRC input order={}'.format(
            args.block_data_id_increment, args.block_crc_order))
        path = Path(args.file)
        if path.suffix.lower() != '.bin':
            raise ValueError('firmware must be a .bin file')
        size = path.stat().st_size
        if not 0 < size <= min(0xFFFFFFFF, 65535 * 128):
            raise ValueError('firmware must be nonempty and fit 65535 blocks')
        firmware = path.read_bytes()
        if len(firmware) != size:
            raise ValueError('firmware changed while reading')
        log.write('Firmware bytes={} blocks={} tail={}'.format(size, (size + 127) // 128, size % 128 or 128))
        check_device_online(args, log, 'before')
        can_diagnostic(args, log, 'before')
        update_failed = False
        completion = None
        try:
            for attempt in (1, 2):
                log.write('UPDATE_ATTEMPT {}/2'.format(attempt))
                try:
                    with SocketCan(args.can) as bus:
                        completion = Updater(bus, args, log).run(firmware)
                    break
                except (OSError, ValueError, ProtocolError) as exc:
                    log.write('UPDATE_ATTEMPT {}/2 failed: {} {}'.format(
                        attempt, type(exc).__name__, exc))
                    if attempt == 2:
                        raise
                    log.write('UPDATE_RETRY waiting 2s before second upgrade')
                    time.sleep(2)
        except (OSError, ValueError, ProtocolError, KeyboardInterrupt) as exc:
            update_failed = True
            log.write('UPDATE_RESULT failed: {} {}'.format(type(exc).__name__, exc))
            raise
        finally:
            can_diagnostic(args, log, 'failed' if update_failed else 'after')
            try:
                check_device_online(args, log, 'after')
            except (OSError, ValueError, ProtocolError, KeyboardInterrupt) as exc:
                log.write('POST_CHECK failed: {} {}'.format(type(exc).__name__, exc))
                if not update_failed and (isinstance(exc, KeyboardInterrupt)
                                          or completion != 'status_query_sent'):
                    raise
        return 0
    except KeyboardInterrupt:
        if log:
            log.write('Interrupted; no cancel command sent; upgrade completion is unknown')
        return 130
    except (OSError, ValueError, ProtocolError) as exc:
        if log:
            log.write('FAILED: {}'.format(exc))
        else:
            print('FAILED: {}'.format(exc), file=sys.stderr)
        return 4 if isinstance(exc, TimeoutError) else 1
    finally:
        if log:
            log.close()


if __name__ == '__main__':
    sys.exit(main())
