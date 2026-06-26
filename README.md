# BSLBATT Venus OS Tool

`bslbatt-tool.py` is a combined Victron GX / Venus OS remote firmware update
tool for BSLBATT BMS devices. It supports both Venus OS operations:

- listing updatable devices on SocketCAN buses;
- updating one selected device with firmware uploaded through VRM.

The tool is designed for the Venus OS remote-toolbox contract. Stdout is
reserved for Venus OS XML only. Debug logs and device error details are written
to stderr.

## Requirements

- Python 3 on Victron GX / Venus OS.
- Linux SocketCAN support.
- A configured CAN interface such as `can0` or `vecan0`.
- The selected GX CAN port must already use the BSLBATT-required CAN bitrate,
  for example `500 kbit/s` in the verified case below, or whatever bitrate is
  required by the target battery.

The script does not change CAN bitrate and does not bring CAN interfaces
up/down.

## Usage

Show help:

```bash
python3 bslbatt-tool.py --help
```

List devices on all available `can*` and `vecan*` interfaces:

```bash
python3 bslbatt-tool.py
```

List devices on `can0`:

```bash
python3 bslbatt-tool.py -c can0
```

The explicit list flag is also supported:

```bash
python3 bslbatt-tool.py --list -c can0
```

Update the selected device using the Venus OS style arguments:

```bash
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/firmware.bin
```

The explicit update flag is optional when `-f` is present, but can be used:

```bash
python3 bslbatt-tool.py -u -c can0 -n 0x0 -f /data/vrmfilescache/firmware.bin
```

Enable debug logs:

```bash
python3 bslbatt-tool.py -c can0 -d
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/firmware.bin -d
```

On Venus OS images where `python` points to Python 3, the same commands can be
called with `python`.

## Options

| Option | Mode | Required | Description |
|--------|------|----------|-------------|
| `-l`, `--list` | List | No | Forces device listing mode. If neither `--list` nor `--update` is provided, listing is inferred unless `-f` is present. |
| `-u`, `--update` | Update | No | Forces firmware update mode. Update mode is also inferred when `-f` is provided. |
| `-c`, `--can` | Both | List: no, update: yes | SocketCAN interface, for example `can0` or `vecan0`. In list mode, omitted means scan all available `can*`/`vecan*` interfaces. |
| `-n`, `--node-id` | Update | Yes | CAN `connection-id` returned from list XML. The current BSLBATT single-device protocol only accepts `0x0`. |
| `--timeout` | List | No | Passive discovery timeout in seconds. Default: `3.0`. |
| `--connection` | Update | No | Legacy connection string, for example `socketcan:can0/0x0`. Prefer `-c/-n` for Venus OS / VRM. |
| `-f`, `--file` | Update | Yes | Absolute path of the firmware file uploaded to GX / VRM cache. Raw firmware files and zip packages are accepted. |
| `-d`, `--debug` | Both | No | Writes debug logs to stderr. |
| `--can-log` | Update | No | Parsed CAN RX/update log file. Default: `venus_firmware_update_can.log`. Use an empty value to disable. |

## Device Discovery

Discovery passively listens for Victron BMS-CAN LV frames on the selected CAN
bus. The script treats a bus as a BSLBATT candidate when it sees the BSLBATT
identity text, the BSLBATT device marker frame, or enough core BMS-CAN frames
without a conflicting identity.

The list output for each discovered device is a single XML element:

```xml
<device serial="ABC123" version="1.23" description="BSLBATT BMS" id="TODO_PRODUCT_ID" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

Important fields:

- `connection-type="can"` tells Venus OS this is a CAN-connected device.
- `connection-id="0x0"` is passed back to update mode as `-n`.
- `connection="socketcan:can0/0x0"` is kept for compatibility with older local
  scripts and can be passed through `--connection`.

No device found is not an error. In that case the tool prints no XML and exits
with code `0`.

Current device metadata parsing:

- firmware version is decoded from CAN ID `0x35F` bytes 2 and 3 as `X.Y`;
- serial number is built from CAN IDs `0x380` and `0x381` when available;
- description is built from device name, manufacturer/family, or model fields
  when available;
- when version, serial, or model frames are not available, the tool still
  reports a BSLBATT candidate with fallback values such as
  `version="unknown"` and `serial="BSLBATT-can0"`;
- the default product id is still `TODO_PRODUCT_ID` in `bslbatt-tool.py` and
  must be replaced with the Victron-assigned Product ID before final delivery.

## Firmware Update Flow

Update mode validates all local inputs before opening CAN. Firmware paths must
be absolute. If the file is a zip package, the tool runs the zip CRC check and
requires exactly one `.bin`, `.fw`, or `.img` payload inside the package.

The implemented BSLBATT CAN upgrade flow is:

1. Resolve target CAN bus and node id from `-c/-n`, or from legacy
   `--connection`.
2. Read and validate the firmware file.
3. Calculate transfer size, frame count, and CRC32 for logging.
4. Open the SocketCAN interface.
5. Drain old control frames from the socket.
6. Send start request `0x18A055AA` with transfer size and frame count.
7. Wait for start ACK `0x18A0AA55`.
8. Send firmware data frames starting at extended CAN ID `0x13000001`.
9. Each data frame carries 7 bytes of firmware data plus a 1-byte checksum.
10. Wait for data ACK `0x18A1AA55` after every 10 data frames.
11. Send finish request `0x18A255AA`.
12. Wait for finish ACK `0x18A2AA55`.

The updater treats BMS error frame `0x18A3AA55` as a device memory/update
error. Error frame details are always written to stderr because they are needed
for diagnosis.

Progress XML emitted during update, abridged:

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

Current status: `locate_device()`, `erase_flash()`, and `verify_firmware()` are
protocol placeholders. The actual transfer is implemented, but product
compatibility checks still need the final BSLBATT firmware package format, such
as magic header, target model, target version, payload length, CRC, or
signature.

## Verified GX Case

The project includes the GX terminal record in `logs/upgrade_case.log`. The
successful case was run on a CCGX where `can0` was already up:

```text
3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 100
    can state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 100
          bitrate 500000 sample-point 0.846
```

Before the update, list mode found one BSLBATT device on `can0`:

```bash
python bslbatt-tool.py -c can0
```

```xml
<device serial="BSLBATT770-can0" version="1.23" description="BSLBATT 770" id="TODO_PRODUCT_ID" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

The update command used the XML `connection-id` value as `-n` and the VRM cache
file as `-f`:

```bash
python bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/124.bin
```

The update printed Venus XML progress from firmware checking through
`Update successful`. During writing it emitted incremental progress levels
`21` through `90`.

After the update, list mode reported the same device at firmware `1.24`:

```xml
<device serial="BSLBATT770-can0" version="1.24" description="BSLBATT 770" id="TODO_PRODUCT_ID" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

Additional observed outputs:

If metadata frames are incomplete, list mode can still report a detected
BSLBATT candidate:

```xml
<device serial="BSLBATT-can0" version="unknown" description="BSLBATT" id="TODO_PRODUCT_ID" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

```bash
python /opt/bs/bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/1123123.bin
```

```xml
<message type="normal">Firmware path error</message>
```

```bash
python /opt/bs/bslbatt-tool.py -c can0 -n 0x2 -f /data/vrmfilescache/48100.bin
```

```xml
<message type="normal">Device id error</message>
```

## Check Available CAN Gateway

On GX, `vup` can show which SocketCAN gateway is available:

```bash
vup --canbus socketcan:can0
```

If `socketcan:vecan0` is not found and GX reports only `socketcan:can0`, use
`can0`:

```bash
python3 bslbatt-tool.py -c can0
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
| 8 | Device memory/update error reported by BMS |
| 9 | Firmware file error |
| 10 | Verification failed |
| 11 | Verification timeout |

List mode uses codes `0`, `1`, `2`, `3`, and `6`. Update mode may use all codes
listed above.

## Final Delivery Checklist

- Replace `TODO_PRODUCT_ID` in `bslbatt-tool.py` with the Victron-assigned
  Product ID.
- Confirm the final BSLBATT firmware package format and add real model/version
  compatibility validation.
- Confirm whether `locate_device()`, `erase_flash()`, and `verify_firmware()`
  should remain no-op steps for the final BSLBATT protocol.
- Run the tool on a GX / Venus OS device with the target BMS and verify
  XML-only stdout.
