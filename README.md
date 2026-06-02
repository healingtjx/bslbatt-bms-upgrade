# BSLBATT Venus OS Tools

This directory contains two Python command-line tools for Victron GX / Venus OS integration:

- `venus_device_list.py`: list updatable BSLBATT devices on a selected CAN interface.
- `venus_firmware_update.py`: update one BSLBATT device with a firmware file uploaded by VRM.

Stdout is reserved for XML consumed by Venus OS / VRM. Debug logs are written to stderr with `-d`.

## Common Commands

Show help:

```bash
python3 venus_device_list.py --help
python3 venus_firmware_update.py --help
```

List devices on `can0`:

```bash
python3 venus_device_list.py --list -c can0
```

List devices on `vecan0`:

```bash
python3 venus_device_list.py --list -c vecan0
```

List devices with debug logs:

```bash
python3 venus_device_list.py --list -c can0 -d
```

List devices with explicit Victron Product ID and manufacturer type:

```bash
python3 venus_device_list.py --list -c can0 --product-id 49188 --type bslbatt
```

Expected list output format:

```xml
<device serial="ABC123" version="1.0.0" description="BSLBATT BMS" id="49188" type="bslbatt" connection="socketcan:can0/0x2A" updatable="True" />
```

Update a device:

```bash
python3 venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/firmware.bin
```

Update a device with debug logs:

```bash
python3 venus_firmware_update.py --update -s socketcan:can0/0x2A -f /data/vrmfilescache/firmware.bin -d
```

Expected update output format:

```xml
<message type="normal">Checking firmware</message>
<progress level="0" />
<message type="normal">Locating device</message>
<progress level="5" />
```

## Check Available CAN Gateway

These tools are intended to run on Victron GX / Venus OS with SocketCAN.

On GX, `vup` can show which SocketCAN gateway is available:

```bash
vup --canbus socketcan:can0
```

If `socketcan:vecan0` is not found and GX reports only `socketcan:can0`, use `can0`:

```bash
python3 venus_device_list.py --list -c can0
```

## CAN Bitrate

Do not change CAN bitrate in these scripts.

If the BSLBATT device requires `250 kbit/s`, configure the selected CAN port in the GX CAN-bus settings before running the tools. The scripts only bind to the CAN interface passed by Venus OS, for example `can0` or `vecan0`.

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
| 9 | File error |
| 10 | Verification failed |
| 11 | Verification timeout |

No device found during list scanning is not an error. The list tool should print nothing and exit with code `0`.

## Items To Replace Before Final Delivery

- Replace `TODO_PRODUCT_ID` in `venus_device_list.py` with the Victron Product ID.
- Implement `decode_bslbatt_device()` in `venus_device_list.py`.
- Implement the real BSLBATT bootloader protocol in `BslbattFirmwareUpdater` in `venus_firmware_update.py`.
- Implement real firmware format validation in `validate_bslbatt_firmware()`.
