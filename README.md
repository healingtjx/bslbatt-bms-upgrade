# BSLBATT Venus OS Tools

This repository contains two Python command-line tools for Victron GX / Venus OS remote firmware update integration:

- `venus_device_list.py`: lists updatable BSLBATT devices on a selected SocketCAN interface.
- `venus_firmware_update.py`: updates one BSLBATT device with a firmware file uploaded through VRM.

Both tools are designed for Venus OS. Stdout is reserved for XML consumed by Venus OS / VRM, and debug logs are written to stderr only when `-d` is enabled.

## Requirements

- Python 3 on Victron GX / Venus OS.
- Linux SocketCAN support.
- A configured CAN interface such as `can0` or `vecan0`.
- The selected GX CAN port must already be configured with the BSLBATT-required CAN bitrate, for example `250 kbit/s`.

The scripts do not change CAN bitrate or bring CAN interfaces up/down.

## Device List Tool

Show help:

```bash
python3 venus_device_list.py --help
```

Scan all available GX CAN interfaces matching `can*` or `vecan*`:

```bash
python3 venus_device_list.py --list
```

The short form is also supported:

```bash
python3 venus_device_list.py -l
```

List devices only on `can0`:

```bash
python3 venus_device_list.py --list -c can0
```

List devices on `vecan0`:

```bash
python3 venus_device_list.py --list -c vecan0
```

Enable debug logs:

```bash
python3 venus_device_list.py --list -c can0 -d
```

Override the Victron Product ID and manufacturer type:

```bash
python3 venus_device_list.py --list -c can0 --product-id 49188 --type bslbatt
```

Options:

| Option | Required | Description |
|--------|----------|-------------|
| `-l`, `--list` | Yes | Runs device listing mode. |
| `-c`, `--can` | No | SocketCAN interface, for example `can0` or `vecan0`. If omitted, the tool scans all available `can*`/`vecan*` interfaces. |
| `--timeout` | No | Passive scan time in seconds. Default: `3.0`. |
| `--product-id` | No | Victron Product ID. Default is currently `TODO_PRODUCT_ID`. |
| `--type` | No | Manufacturer type used by VRM. Default: `bslbatt`. |
| `-d`, `--debug` | No | Writes debug logs to stderr. |

Expected XML output for each discovered device:

```xml
<device serial="ABC123" version="1.0.0" description="BSLBATT BMS" id="49188" type="bslbatt" connection="socketcan:can0/0x2A" updatable="True" />
```

The `connection` value is generated as `socketcan:<interface>/<node_id>` and is passed back to the firmware update tool through `-s`.

No device found is not an error. In that case the list tool prints no XML and exits with code `0`.

With `-d`, received CAN frames are written to stderr for protocol debugging. They are never written to stdout.

Current status: `decode_bslbatt_device()` is still a placeholder. It must be replaced with the actual BSLBATT CAN identification/version parsing before final delivery.

## Firmware Update Tool

Show help:

```bash
python3 venus_firmware_update.py --help
```

Update a device:

```bash
python3 venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/firmware.bin
```

Update with debug logs:

```bash
python3 venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/firmware.bin -d
```

Options:

| Option | Required | Description |
|--------|----------|-------------|
| `--update` | Yes | Runs firmware update mode. |
| `-s`, `--connection` | Yes | Connection from list XML, for example `socketcan:can0/0x2A`. |
| `-f`, `--file` | Yes | Absolute path of the firmware file uploaded to GX / VRM cache. Raw firmware files and zip packages are accepted. |
| `-d`, `--debug` | No | Writes debug logs and CAN TX/RX traces to stderr. |

The connection format must be:

```text
socketcan:<can-interface>/<node-id>
```

Example:

```text
socketcan:can0/0x2A
```

## Firmware Update Flow

`venus_firmware_update.py` currently implements the BSLBATT CAN upgrade transfer flow:

1. Validate the `socketcan:<interface>/<node-id>` connection string.
2. Read the firmware file from an absolute path and validate that it exists and is non-empty.
3. If the uploaded file is a zip package, run the zip CRC check and extract exactly one `.bin`, `.fw`, or `.img` firmware payload.
4. Check that the resulting firmware fits the BSLBATT CAN transfer size and frame-count fields.
5. Calculate firmware size and CRC32 for debug logging.
6. Open the SocketCAN interface from `-s`.
7. Send start request `0x18A055AA` with transfer size and frame count.
8. Wait for start ACK `0x18A0AA55`.
9. Send firmware data frames starting at extended CAN ID `0x13000001`.
10. Each data frame carries 7 bytes of firmware data plus a 1-byte checksum.
11. Wait for data ACK `0x18A1AA55` every 64 frames.
12. Send finish request `0x18A255AA`.
13. Wait for finish ACK `0x18A2AA55`.

The updater treats BMS error frame `0x18A3AA55` as a device memory error.

Progress XML is emitted during the update:

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

Current status: file integrity checks cover readable raw files, empty files, zip package CRC, and transfer-size limits. Product compatibility checks still require the real BSLBATT package format, such as magic header, target model, target version, payload length, CRC, or signature.

## Check Available CAN Gateway

On GX, `vup` can show which SocketCAN gateway is available:

```bash
vup --canbus socketcan:can0
```

If `socketcan:vecan0` is not found and GX reports only `socketcan:can0`, use `can0`:

```bash
python3 venus_device_list.py --list -c can0
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | CAN init error |
| 3 | CAN communication error |
| 4 | Timeout |
| 5 | Firmware error or incompatible firmware |
| 6 | Argument error |
| 7 | Device not found |
| 8 | Device memory error |
| 9 | Firmware file error |
| 10 | Verification failed |
| 11 | Verification timeout |

The list tool uses codes `0`, `1`, `2`, `3`, and `6`. The firmware update tool may use all codes listed above.

## Final Delivery Checklist

- Replace `TODO_PRODUCT_ID` in `venus_device_list.py` with the Victron-assigned Product ID.
- Implement `decode_bslbatt_device()` in `venus_device_list.py`.
- Add real BSLBATT model/version compatibility validation once the firmware package format is confirmed.
- Confirm whether `locate_device()`, `erase_flash()`, and `verify_firmware()` should remain no-op steps for the final BSLBATT protocol.
- Run the tools on a GX / Venus OS device with the target BMS and verify XML-only stdout.
