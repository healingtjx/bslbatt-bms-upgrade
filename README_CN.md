# BSLBATT Venus OS 对接工具

`bslbatt-tool.py` 是一个合并后的 Victron GX / Venus OS 远程固件升级对接工具，
用于 BSLBATT BMS 设备，支持 Venus OS 需要的两个操作：

- 在 SocketCAN 总线上列出可升级设备；
- 使用 VRM 上传到 GX 的固件文件升级选中的单个设备。

该工具面向 Venus OS remote-toolbox 调用契约。stdout 只输出 Venus OS 读取的
XML；调试日志和设备错误详情输出到 stderr。

## 运行要求

- Victron GX / Venus OS 上的 Python 3。
- Linux SocketCAN 支持。
- 已配置的 CAN 接口，例如 `can0` 或 `vecan0`。
- GX 上选中的 CAN 口必须提前配置成 BSLBATT 设备要求的波特率，例如
  下方实测案例中的 `500 kbit/s`，或目标电池实际要求的波特率。

脚本不会修改 CAN 波特率，也不会负责启停 CAN 接口。

## 使用方式

查看帮助：

```bash
python3 bslbatt-tool.py --help
```

扫描所有可用的 `can*` 和 `vecan*` 接口：

```bash
python3 bslbatt-tool.py
```

只扫描 `can0`：

```bash
python3 bslbatt-tool.py -c can0
```

也可以显式指定列表模式：

```bash
python3 bslbatt-tool.py --list -c can0
```

按 Venus OS 调用方式升级选中设备：

```bash
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/firmware.bin
```

传入 `-f` 时会自动推断为升级模式，也可以显式加 `--update`：

```bash
python3 bslbatt-tool.py --update -c can0 -n 0x0 -f /data/vrmfilescache/firmware.bin
```

开启调试日志：

```bash
python3 bslbatt-tool.py -c can0 -d
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/firmware.bin -d
```

如果 Venus OS 镜像中的 `python` 指向 Python 3，也可以直接用 `python` 调用同样
的命令。

## 参数说明

| 参数 | 模式 | 是否必填 | 说明 |
|------|------|----------|------|
| `-l`, `--list` | 列表 | 否 | 强制执行设备列表模式。不传 `--list`/`--update` 时，除非提供 `-f`，否则默认推断为列表模式。 |
| `--update` | 升级 | 否 | 强制执行固件升级模式。提供 `-f` 时也会自动推断为升级模式。 |
| `-c`, `--can` | 两者 | 列表否，升级是 | SocketCAN 接口，例如 `can0` 或 `vecan0`。列表模式不传时扫描所有可用的 `can*`/`vecan*` 接口。 |
| `-n`, `--node-id` | 升级 | 是 | 设备列表 XML 返回的 CAN `connection-id`。当前 BSLBATT 单设备协议只接受 `0x0`。 |
| `--timeout` | 列表 | 否 | 被动扫描时长，单位秒。默认：`3.0`。 |
| `--connection` | 升级 | 否 | 兼容旧脚本的连接字符串，例如 `socketcan:can0/0x0`。Venus OS / VRM 优先使用 `-c/-n`。 |
| `-f`, `--file` | 升级 | 是 | 上传到 GX / VRM 缓存目录的固件文件绝对路径。支持原始固件文件和 zip 包。 |
| `-d`, `--debug` | 两者 | 否 | 将调试日志输出到 stderr。 |
| `--can-log` | 升级 | 否 | 升级期间解析后的 CAN 收发日志文件。默认：`venus_firmware_update_can.log`。传空值可关闭。 |

## 设备发现

发现逻辑会在指定 CAN 总线上被动监听 Victron BMS-CAN LV 帧。当工具看到
BSLBATT 身份文本、BSLBATT 设备标记帧，或看到足够的核心 BMS-CAN 帧且没有冲突
身份信息时，会把该总线视为 BSLBATT 候选设备。

每个发现设备会输出一行 XML：

```xml
<device serial="ABC123" version="v1.23" description="BSLBATT BMS" id="TODO_PRODUCT_ID" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

关键字段：

- `connection-type="can"` 表示该设备通过 CAN 连接。
- `connection-id="0x0"` 会在升级时作为 `-n` 回传。
- `connection="socketcan:can0/0x0"` 用于兼容旧本地脚本，也可通过
  `--connection` 传入升级模式。

扫描不到设备不是错误。此时工具不输出 XML，并以退出码 `0` 结束。

当前设备元数据解析：

- 固件版本从 CAN ID `0x35F` 的第 2、3 字节解析为 `vX.Y`；
- 序列号优先由 CAN ID `0x380` 和 `0x381` 拼接；
- 描述优先使用设备名称，其次使用厂商、系列或型号字段；
- 当版本、序列号或型号帧不完整时，工具仍会输出 BSLBATT 候选设备，并使用
  `version="unknown"`、`serial="BSLBATT-can0"` 这类兜底值；
- `bslbatt-tool.py` 中的默认 Product ID 仍是 `TODO_PRODUCT_ID`，最终交付前
  必须替换为 Victron 分配的 Product ID。

## 固件升级流程

升级模式会先校验本地参数和文件，再打开 CAN。固件路径必须是绝对路径。如果上传
文件是 zip 包，工具会先执行 zip CRC 检查，并要求包内只有一个 `.bin`、`.fw`
或 `.img` 固件载荷。

当前已实现的 BSLBATT CAN 升级流程：

1. 从 `-c/-n` 解析目标 CAN 总线和节点 ID，或从兼容参数 `--connection` 解析。
2. 读取并校验固件文件。
3. 计算传输大小、帧数和 CRC32，用于日志。
4. 打开 SocketCAN 接口。
5. 清空旧控制帧，避免干扰本次升级。
6. 发送开始请求 `0x18A055AA`，数据包含传输大小和总帧数。
7. 等待开始 ACK `0x18A0AA55`。
8. 从扩展 CAN ID `0x13000001` 开始发送固件数据帧。
9. 每个数据帧携带 7 字节固件数据和 1 字节校验和。
10. 每 10 个数据帧等待一次数据 ACK `0x18A1AA55`。
11. 发送结束请求 `0x18A255AA`。
12. 等待结束 ACK `0x18A2AA55`。

升级器会把 BMS 错误帧 `0x18A3AA55` 视为设备存储器或升级异常。错误帧详情会
始终输出到 stderr，便于排查。

升级过程中会输出 XML 进度，以下为省略后的示例：

```xml
<message type="normal">Checking firmware</message>
<progress level="0" />
<message type="normal">Locating device</message>
<progress level="5" />
<message type="normal">Entering bootloader</message>
<progress level="10" />
<message type="normal">Erasing device</message>
<progress level="20" />
<message type="normal">Writing firmware</message>
<progress level="21" />
...
<progress level="90" />
<message type="normal">Verifying firmware</message>
<progress level="98" />
<message type="normal">Starting application</message>
<progress level="100" />
<message type="normal">Update successful</message>
```

当前状态：`locate_device()`、`erase_flash()` 和 `verify_firmware()` 仍是协议
占位步骤。实际 CAN 传输流程已实现，但产品兼容性仍需要最终 BSLBATT 固件包格式，
例如包头 magic、目标型号、目标版本、载荷长度、CRC 或签名。

## GX 实测案例

项目中保留了 GX 终端记录 `logs/upgrade_case.log`。成功案例运行在 CCGX 上，
当时 `can0` 已经处于 UP 状态：

```text
3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 100
    can state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 100
          bitrate 500000 sample-point 0.846
```

升级前，列表模式在 `can0` 上发现了一个 BSLBATT 设备：

```bash
python bslbatt-tool.py -c can0
```

```xml
<device serial="model770-can0" version="v1.23" description="model 770" id="TODO_PRODUCT_ID" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

升级命令使用列表 XML 中的 `connection-id` 作为 `-n`，使用 VRM 缓存目录中的固件
文件作为 `-f`：

```bash
python bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/124.bin
```

升级过程中 stdout 从 `Checking firmware` 到 `Update successful` 持续输出 Venus
XML。写入阶段会输出 `21` 到 `90` 的递增进度。

升级后，列表模式报告同一设备固件版本变为 `v1.24`：

```xml
<device serial="model770-can0" version="v1.24" description="model 770" id="TODO_PRODUCT_ID" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

其他实测输出：

当元数据帧不完整时，列表模式仍可以报告已检测到的 BSLBATT 候选设备：

```xml
<device serial="BSLBATT-can0" version="unknown" description="BSLBATT" id="TODO_PRODUCT_ID" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

固件路径不存在时：

```bash
python /opt/bs/bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/1123123.bin
```

```xml
<message type="normal">Firmware path error</message>
```

设备 ID 不是当前单设备协议支持的 `0x0` 时：

```bash
python /opt/bs/bslbatt-tool.py -c can0 -n 0x2 -f /data/vrmfilescache/48100.bin
```

```xml
<message type="normal">Device id error</message>
```

## 检查 GX 上可用的 CAN 网关

可以在 GX 上用 `vup` 查看当前可用的 SocketCAN 网关：

```bash
vup --canbus socketcan:can0
```

如果执行 `socketcan:vecan0` 时提示找不到，而设备只列出 `socketcan:can0`，就
应该使用 `can0`：

```bash
python3 bslbatt-tool.py -c can0
```

## 退出码

| 错误码 | 说明 |
|--------|------|
| 0 | 成功 |
| 1 | 通用错误 |
| 2 | CAN 初始化错误 |
| 3 | CAN 通信错误 |
| 4 | 超时 |
| 5 | 固件错误或固件不兼容 |
| 6 | 参数错误 |
| 7 | 未找到设备 |
| 8 | BMS 上报的设备存储器或升级异常 |
| 9 | 固件文件错误 |
| 10 | 升级验证失败 |
| 11 | 升级验证超时 |

列表模式使用错误码 `0`、`1`、`2`、`3` 和 `6`。升级模式可能使用上表中的全部
错误码。

## 最终交付检查项

- 将 `bslbatt-tool.py` 中的 `TODO_PRODUCT_ID` 替换为 Victron 分配的 Product ID。
- 确认最终 BSLBATT 固件包格式，并补充真实型号和版本兼容性校验。
- 确认 `locate_device()`、`erase_flash()` 和 `verify_firmware()` 在最终
  BSLBATT 协议中是否应继续保持空操作。
- 在 GX / Venus OS 设备和目标 BMS 上实测工具，并确认 stdout 只输出 XML。
