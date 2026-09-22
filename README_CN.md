# BSLBATT Venus OS 对接工具

[English](README.md) | 简体中文

`bslbatt-tool.py` 是一个合并后的 Victron GX / Venus OS 远程固件升级对接工具，
用于 BSLBATT BMS 设备，支持 Venus OS 需要的两个操作：

- 在 SocketCAN 总线上列出可升级设备；
- 使用 VRM 上传到 GX 的固件文件升级选中的单个设备。

该工具面向 Venus OS remote-toolbox 调用契约。stdout 只输出 Venus OS 读取的
XML；调试日志和设备错误详情输出到 stderr。

## 支持产品与固件

- 支持型号：仅支持 `BSL 16串`。
- Victron Product ID：`0xB021`。
- CAN connection-id：当前单设备协议固定为 `0x0`。
- 支持远程升级的最低当前固件版本：`V1.245`。
- 测试固件版本：`V1.245` 和 `V1.246`。
- 支持 `V1.245 → V1.246` 升级，也支持 `V1.246 → V1.245` 降级。
- 固件文件名使用 `V1.245`/`V1.246`，设备发现按 GX 设备显示规则输出
  `12.45`/`12.46`。

## 运行要求

- Victron GX / Venus OS 上的 Python 3，仅依赖 Python 标准库。
- Linux SocketCAN 支持。
- 已配置的 CAN 接口，例如 `can0` 或 `vecan0`。
- GX 上选中的 CAN 口必须提前配置成 BSLBATT 设备要求的波特率，以目标电池实际要求为准。

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
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin
```

传入 `-f` 时会自动推断为升级模式，也可以显式加 `--update`：

```bash
python3 bslbatt-tool.py -u -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin
```

也可将完整连接字符串作为 `-c` 传入，此时无需单独传 `-n`：

```bash
python3 bslbatt-tool.py -u -c socketcan:can0/0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin
```

开启调试日志：

```bash
python3 bslbatt-tool.py -c can0 -d
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin -d
```

如果 Venus OS 镜像中的 `python` 指向 Python 3，也可以直接用 `python` 调用同样
的命令。

## 参数说明

| 参数 | 模式 | 是否必填 | 说明 |
|------|------|----------|------|
| `-l`, `--list` | 列表 | 否 | 强制执行设备列表模式。不传 `--list`/`--update` 时，除非提供 `-f`，否则默认推断为列表模式。 |
| `-u`, `--update` | 升级 | 否 | 强制执行固件升级模式。提供 `-f` 时也会自动推断为升级模式。 |
| `-c`, `--can` | 两者 | 列表否，升级是 | SocketCAN 接口，例如 `can0` 或 `vecan0`。列表模式不传时扫描所有可用的 `can*`/`vecan*` 接口。 |
| `-n`, `--node-id` | 升级 | 使用接口名时必填 | 设备列表 XML 返回的 CAN `connection-id`。当前 BSLBATT 单设备协议只接受 `0x0`；使用完整连接字符串时可省略。 |
| `--timeout` | 列表 | 否 | 被动扫描时长，单位秒。默认：`3.0`。 |
| `--connection` | 升级 | 否 | 兼容旧脚本的连接字符串，例如 `socketcan:can0/0x0`。Venus OS / VRM 优先使用 `-c/-n`。 |
| `-f`, `--file` | 升级 | 是 | 上传到 GX / VRM 缓存目录的固件文件绝对路径。支持原始固件文件和 zip 包。 |
| `-d`, `--debug` | 两者 | 否 | 将调试日志输出到 stderr。 |
| `--can-log` | 升级 | 否 | 升级期间解析后的 CAN 收发日志文件。默认：`venus_firmware_update_can.log`。传空值可关闭。 |

## 日志

调试输出默认关闭，`-d` 启用 stderr 诊断。升级时 CAN 文件日志仍默认追加写入当前
工作目录的 `venus_firmware_update_can.log`；可用 `--can-log /data/bslbatt-can.log`
指定位置，或用 `--can-log ""` 关闭文件日志：

```bash
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin --can-log ""
```

关闭文件日志后，升级器不再收集和格式化逐帧日志；单独启用 `-d` 也不会向 stderr
输出逐帧明细。设备错误仍会输出到 stderr，stdout 保持 XML 消息及进度输出。

## 设备发现

发现逻辑会在指定 CAN 总线上被动监听 Victron BMS-CAN LV 帧。当工具看到
BSLBATT 身份文本、BSLBATT 设备标记帧，或看到足够的核心 BMS-CAN 帧且没有冲突
身份信息时，会把该总线视为 BSLBATT 候选设备。

每个发现设备会输出一行 XML：

```xml
<device serial="ABC123" version="12.45" description="BSLBATT BMS" id="0xB021" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

关键字段：

- `connection-type="can"` 表示该设备通过 CAN 连接。
- `connection-id="0x0"` 会在升级时作为 `-n` 回传。
- `connection="socketcan:can0/0x0"` 用于兼容旧本地脚本，也可通过
  `--connection` 传入升级模式。

扫描不到设备不是错误。此时工具不输出 XML，并以退出码 `0` 结束。

当前设备元数据解析：

- 固件版本从 CAN ID `0x35F` 的第 2、3 字节解析为 `X.Y`；
- 序列号优先由 CAN ID `0x380` 和 `0x381` 拼接；
- 描述优先使用设备名称，其次使用厂商、系列或型号字段；
- 当版本、序列号或型号帧不完整时，工具仍会输出 BSLBATT 候选设备，并使用
  `version="unknown"`、`serial="BSLBATT-can0"` 这类兜底值；
- Victron 分配的 Product ID 为 `0xB021`。

## 固件升级流程

固件路径必须为绝对路径。文件名没有前缀限制，支持 VRM 重命名后的无扩展名缓存文件。
ZIP 内仍需包含唯一的 `.bin`、`.fw` 或 `.img` 载荷。打开 CAN 前检查 ZIP CRC、固件非空以及大小不超过
`65535 × 128` 字节。调用示例：
`-c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin`。

`bslbatt-tool.py` 内置升级协议，支持单文件部署，执行流程如下：

1. 通过扩展帧 `0x4610` 发送实际固件长度，等待 `0x4621/A1` 协商 128 字节分包。
2. 每包发送小端包号 `0x4630`、16 帧 `0x4650` 数据和 `0x4670` CRC16/Modbus，
   等待 `0x4681/A2`。尾包用 `FF` 补齐，分包 CRC 覆盖补齐后的 128 字节，大小字段为零。
3. 通过 `0x4690` 发送实际固件内容的 CRC16，等待 `0x46A1/A3`。
4. 发送重启命令 `0x46B0`，等待 `0x46C1/0A` 或 `0B`。
5. 等待 15 秒，成功发送首次 `0x46D0` 后进度为 100%，返回码为 0。
   此判据表示流程完成，**设备最终状态未确认**，不等待 `0D`。

控制帧补零至 8 字节；按照成功参考抓包，包内帧间隔为 2ms，大小 ACK 后等待 48ms，
分包 ACK 后等待 47ms，校验及重启前各等待 32ms。其他阶段 ACK 超时为 300 秒。
每个分包只发送一次；分包 CRC 发出后最多等待 300 秒，只有收到 `0x4681/A2` 才发送下一包。
**超时或设备明确返回错误时立即结束升级，不重发当前分包。**
等待期间收到的帧分批写入日志，避免忙碌总线导致日志缓存溢出。

打开 CAN 执行升级前，工具会临时停止当前接口对应的 Venus OS
`can-bus-bms.<接口>` 服务，避免其周期发送的 `0x305/0x307` 帧干扰 Bootloader 传输。
最后一个固件分包确认后会立即恢复该服务，再继续执行 CRC 校验和启动应用；若传输提前
失败、超时、Ctrl+C 或 SIGTERM，也会兜底恢复。服务目录不存在时跳过。

stdout 仅输出 XML 消息及进度：传输阶段 0～90，CRC 校验通过后 95，首次状态查询
发送成功后 100。完成消息为 `Update flow completed; device final status unconfirmed`。
`--can-log` 记录收发帧及时序，`--debug` 将调试信息写 stderr；日志写入失败不终止升级。
这些检查不代表已验证固件与硬件的兼容性或固件真实性。

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
| 0 | 列表完成；升级流程完成，但设备最终状态未确认 |
| 1 | 通用错误 |
| 2 | CAN 初始化错误 |
| 3 | CAN 通信错误 |
| 4 | 超时 |
| 5 | 固件错误或固件不兼容 |
| 6 | 参数错误 |
| 7 | 保留：未找到设备 |
| 8 | BMS 上报的设备存储器或升级异常 |
| 9 | 固件文件错误 |
| 10 | 升级验证失败 |
| 11 | 保留：升级验证超时 |

列表模式使用错误码 `0`、`1`、`2`、`3` 和 `6`。当前升级流程不返回保留码 `7`、`11`，
ACK 超时统一返回 `4`。升级期间按 Ctrl+C 中断返回 `130`；命令行解析错误由 argparse 返回 `2`。
