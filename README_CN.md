# BSLBATT Venus OS 对接工具

本仓库包含两个用于 Victron GX / Venus OS 远程固件升级对接的 Python 命令行工具：

- `venus_device_list.py`：在指定 SocketCAN 接口上列出可升级的 BSLBATT 设备。
- `venus_firmware_update.py`：使用 VRM 上传到 GX 的固件文件升级单个 BSLBATT 设备。

两个工具都面向 Venus OS 运行环境。stdout 只输出 Venus OS / VRM 需要读取的 XML；调试日志仅在启用 `-d` 时输出到 stderr。

## 运行要求

- Victron GX / Venus OS 上的 Python 3。
- Linux SocketCAN 支持。
- 已配置的 CAN 接口，例如 `can0` 或 `vecan0`。
- GX 上选中的 CAN 口必须提前配置成 BSLBATT 设备要求的波特率，例如 `250 kbit/s`。

脚本不会修改 CAN 波特率，也不会负责启停 CAN 接口。

## 设备列表工具

查看帮助：

```bash
python3 venus_device_list.py --help
```

扫描所有 GX 上可用的 `can*` 或 `vecan*` CAN 接口：

```bash
python3 venus_device_list.py --list
```

也支持短参数：

```bash
python3 venus_device_list.py -l
```

只在 `can0` 上扫描设备：

```bash
python3 venus_device_list.py --list -c can0
```

在 `vecan0` 上扫描设备：

```bash
python3 venus_device_list.py --list -c vecan0
```

开启调试日志：

```bash
python3 venus_device_list.py --list -c can0 -d
```

覆盖 Victron Product ID 和厂商类型：

```bash
python3 venus_device_list.py --list -c can0 --product-id 49188 --type bslbatt
```

参数说明：

| 参数 | 是否必填 | 说明 |
|------|----------|------|
| `-l`, `--list` | 是 | 执行设备列表模式。 |
| `-c`, `--can` | 否 | SocketCAN 接口，例如 `can0` 或 `vecan0`。不传时扫描所有可用的 `can*`/`vecan*` 接口。 |
| `--timeout` | 否 | 被动扫描时长，单位秒。默认：`3.0`。 |
| `--product-id` | 否 | Victron Product ID。当前默认值仍是 `TODO_PRODUCT_ID`。 |
| `--type` | 否 | VRM 使用的厂商类型。默认：`bslbatt`。 |
| `-d`, `--debug` | 否 | 将调试日志输出到 stderr。 |

每个发现设备的期望 XML 输出：

```xml
<device serial="ABC123" version="1.0.0" description="BSLBATT BMS" id="49188" type="bslbatt" connection="socketcan:can0/0x2A" updatable="True" />
```

`connection` 会按 `socketcan:<接口>/<节点ID>` 生成，并在升级时通过 `-s` 回传给固件升级工具。

扫描不到设备不是错误。此时设备列表工具不输出 XML，并以退出码 `0` 结束。

启用 `-d` 时，收到的 CAN 帧会输出到 stderr，便于调试真实设备识别协议；这些内容不会输出到 stdout。

当前状态：`decode_bslbatt_device()` 仍是占位函数。最终交付前必须替换为真实的 BSLBATT CAN 设备识别和版本解析逻辑。

## 固件升级工具

查看帮助：

```bash
python3 venus_firmware_update.py --help
```

升级设备：

```bash
python3 venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/firmware.bin
```

升级设备并开启调试日志：

```bash
python3 venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/firmware.bin -d
```

参数说明：

| 参数 | 是否必填 | 说明 |
|------|----------|------|
| `--update` | 是 | 执行固件升级模式。 |
| `-s`, `--connection` | 是 | 来自设备列表 XML 的连接信息，例如 `socketcan:can0/0x2A`。 |
| `-f`, `--file` | 是 | 上传到 GX / VRM 缓存目录的固件文件绝对路径。支持原始固件文件和 zip 包。 |
| `-d`, `--debug` | 否 | 将调试日志和 CAN 收发帧输出到 stderr。 |

连接格式必须是：

```text
socketcan:<CAN接口>/<节点ID>
```

示例：

```text
socketcan:can0/0x2A
```

## 固件升级流程

`venus_firmware_update.py` 当前已实现 BSLBATT CAN 升级传输流程：

1. 校验 `socketcan:<接口>/<节点ID>` 连接字符串。
2. 从绝对路径读取固件文件，并校验文件存在且非空。
3. 如果上传文件是 zip 包，先执行 zip CRC 校验，并要求包内只有一个 `.bin`、`.fw` 或 `.img` 固件载荷。
4. 校验固件大小和帧数是否符合 BSLBATT CAN 传输字段范围。
5. 计算固件大小和 CRC32，用于调试日志。
6. 根据 `-s` 打开 SocketCAN 接口。
7. 发送开始请求 `0x18A055AA`，数据包含传输大小和总帧数。
8. 等待开始 ACK `0x18A0AA55`。
9. 从扩展 CAN ID `0x13000001` 开始发送固件数据帧。
10. 每个数据帧携带 7 字节固件数据和 1 字节校验和。
11. 每 64 帧等待一次数据 ACK `0x18A1AA55`。
12. 发送结束请求 `0x18A255AA`。
13. 等待结束 ACK `0x18A2AA55`。

升级器会把 BMS 错误帧 `0x18A3AA55` 视为设备存储器错误。

升级过程中会输出 XML 进度：

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
<progress level="90" />
<message type="normal">Verifying firmware</message>
<progress level="98" />
<message type="normal">Starting application</message>
<progress level="100" />
<message type="normal">Update successful</message>
```

当前状态：文件完整性检查已覆盖原始文件可读性、空文件、zip 包 CRC 和传输大小限制。产品兼容性仍需要真实 BSLBATT 固件包格式，例如包头 magic、目标型号、目标版本、载荷长度、CRC 或签名。

## 检查 GX 上可用的 CAN 网关

可以在 GX 上用 `vup` 查看当前可用的 SocketCAN 网关：

```bash
vup --canbus socketcan:can0
```

如果执行 `socketcan:vecan0` 时提示找不到，而设备只列出 `socketcan:can0`，就应该使用 `can0`：

```bash
python3 venus_device_list.py --list -c can0
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
| 8 | 设备存储器错误 |
| 9 | 固件文件错误 |
| 10 | 升级验证失败 |
| 11 | 升级验证超时 |

设备列表工具只使用错误码 `0`、`1`、`2`、`3` 和 `6`。固件升级工具可能使用上表中的全部错误码。

## 最终交付检查项

- 将 `venus_device_list.py` 中的 `TODO_PRODUCT_ID` 替换为 Victron 分配的 Product ID。
- 实现 `venus_device_list.py` 中的 `decode_bslbatt_device()`。
- 在确认 BSLBATT 固件包格式后，补充真实型号和版本兼容性校验。
- 确认 `locate_device()`、`erase_flash()` 和 `verify_firmware()` 在最终 BSLBATT 协议中是否应继续保持空操作。
- 在 GX / Venus OS 设备和目标 BMS 上实测两个工具，并确认 stdout 只输出 XML。
