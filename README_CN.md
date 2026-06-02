# BSLBATT Venus OS 对接工具

本目录包含两个用于 Victron GX / Venus OS 对接的 Python 命令行工具：

- `venus_device_list.py`：在指定 CAN 接口上扫描可升级的 BSLBATT 设备。
- `venus_firmware_update.py`：对 VRM 上传到 GX 的固件文件执行设备升级。

注意：stdout 只用于输出 Venus OS / VRM 需要读取的 XML。调试日志请使用 `-d` 输出到 stderr。

## 常用命令

查看帮助：

```bash
python3 venus_device_list.py --help
python3 venus_firmware_update.py --help
```

在 `can0` 上扫描设备：

```bash
python3 venus_device_list.py --list -c can0
```

在 `vecan0` 上扫描设备：

```bash
python3 venus_device_list.py --list -c vecan0
```

扫描设备并打开调试日志：

```bash
python3 venus_device_list.py --list -c can0 -d
```

扫描设备时显式指定 Victron Product ID 和厂商类型：

```bash
python3 venus_device_list.py --list -c can0 --product-id 49188 --type bslbatt
```

设备扫描期望输出格式：

```xml
<device serial="ABC123" version="1.0.0" description="BSLBATT BMS" id="49188" type="bslbatt" connection="socketcan:can0/0x2A" updatable="True" />
```

升级设备：

```bash
python3 venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/firmware.bin
```

升级设备并打开调试日志：

```bash
python3 venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/firmware.bin -d
```

升级过程期望输出格式：

```xml
<message type="normal">Checking firmware</message>
<progress level="0" />
<message type="normal">Locating device</message>
<progress level="5" />
```

## 检查 GX 上可用的 CAN 网关

这两个工具用于 Victron GX / Venus OS 环境，并通过 SocketCAN 使用 GX 上的 CAN 接口。

可以在 GX 上用 `vup` 查看当前可用的 SocketCAN 网关：

```bash
vup --canbus socketcan:can0
```

如果执行 `socketcan:vecan0` 时提示找不到，而设备只列出 `socketcan:can0`，就应该使用 `can0`：

```bash
python3 venus_device_list.py --list -c can0
```

## CAN 波特率说明

脚本不要修改 CAN 总线波特率。

如果 BSLBATT 设备只支持 `250 kbit/s`，需要安装人员提前在 GX 的 CAN-bus 设置中把对应 CAN 口配置成 `250 kbit/s`。脚本只绑定 Venus OS 传入的 CAN 接口，例如 `can0` 或 `vecan0`。

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
| 9 | 文件错误 |
| 10 | 升级验证失败 |
| 11 | 升级验证超时 |

扫描时没有发现设备不是错误。设备扫描工具应不输出任何 XML，并以退出码 `0` 结束。

## 最终交付前需要替换或实现

- 将 `venus_device_list.py` 中的 `TODO_PRODUCT_ID` 替换为 Victron 分配的 Product ID。
- 实现 `venus_device_list.py` 中的 `decode_bslbatt_device()`，填入真实 BSLBATT 设备识别和版本解析逻辑。
- 实现 `venus_firmware_update.py` 中的 `BslbattFirmwareUpdater`，填入真实 BSLBATT bootloader 升级协议。
- 实现 `venus_firmware_update.py` 中的 `validate_bslbatt_firmware()`，填入真实固件格式、型号、版本、CRC 或签名校验逻辑。
