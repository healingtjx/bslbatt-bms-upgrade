# BSLBATT Venus OS Tool

English | [简体中文](README_CN.md)

`bslbatt-tool.py` is a combined Victron GX / Venus OS remote firmware update
tool for BSLBATT BMS devices. It supports both Venus OS operations:

- listing updatable devices on SocketCAN buses;
- updating one selected device with firmware uploaded through VRM.

The tool is designed for the Venus OS remote-toolbox contract. Stdout is
reserved for Venus OS XML only. Debug logs and device error details are written
to stderr.

## Supported Product and Firmware

- Supported model: `BSL 16-series (BSL 16串)` only.
- Victron Product ID: `0xB021`.
- CAN connection id: fixed at `0x0` by the current single-device protocol.
- Minimum installed firmware version required for remote update: `V1.245`.
- Test firmware versions: `V1.245` and `V1.246`.
- Both `V1.245 -> V1.246` upgrade and `V1.246 -> V1.245` downgrade are supported.
- Firmware filenames use `V1.245`/`V1.246`; device discovery reports these
  versions as `12.45`/`12.46`, matching the GX device display.

## Requirements

- Python 3 on Victron GX / Venus OS; only the Python standard library is required.
- Linux SocketCAN support.
- A configured CAN interface such as `can0` or `vecan0`.
- The selected GX CAN port must already use the BSLBATT-required CAN bitrate
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
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin
```

The explicit update flag is optional when `-f` is present, but can be used:

```bash
python3 bslbatt-tool.py -u -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin
```

You can also pass the full connection string as `-c`, without a separate `-n`:

```bash
python3 bslbatt-tool.py -u -c socketcan:can0/0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin
```

Enable debug logs:

```bash
python3 bslbatt-tool.py -c can0 -d
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin -d
```

On Venus OS images where `python` points to Python 3, the same commands can be
called with `python`.

## Options

| Option | Mode | Required | Description |
|--------|------|----------|-------------|
| `-l`, `--list` | List | No | Forces device listing mode. If neither `--list` nor `--update` is provided, listing is inferred unless `-f` is present. |
| `-u`, `--update` | Update | No | Forces firmware update mode. Update mode is also inferred when `-f` is provided. |
| `-c`, `--can` | Both | List: no, update: yes | SocketCAN interface, for example `can0` or `vecan0`. In list mode, omitted means scan all available `can*`/`vecan*` interfaces. |
| `-n`, `--node-id` | Update | With a bare interface name | CAN `connection-id` returned from list XML. The current BSLBATT single-device protocol only accepts `0x0`; omit when using a full connection string. |
| `--timeout` | List | No | Passive discovery timeout in seconds. Default: `3.0`. |
| `--connection` | Update | No | Legacy connection string, for example `socketcan:can0/0x0`. Prefer `-c/-n` for Venus OS / VRM. |
| `-f`, `--file` | Update | Yes | Absolute path of the firmware file uploaded to GX / VRM cache. Raw firmware files and zip packages are accepted. |
| `-d`, `--debug` | Both | No | Writes debug logs to stderr. |
| `--can-log` | Update | No | Parsed CAN RX/update log file. Default: `venus_firmware_update_can.log`. Use an empty value to disable. |

## Logging

Debug output is disabled by default; `-d` enables stderr diagnostics. During an
update, CAN file logging still defaults to appending to
`venus_firmware_update_can.log` in the current working directory. Set
`--can-log /data/bslbatt-can.log` to choose a location, or disable file logging:

```bash
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin --can-log ""
```

With file logging disabled, the updater skips per-frame log collection and
formatting. Enabling `-d` alone does not print individual frames to stderr.
Device errors still go to stderr; stdout retains XML messages and progress.

## Device Discovery

Discovery passively listens for Victron BMS-CAN LV frames on the selected CAN
bus. The script treats a bus as a BSLBATT candidate when it sees the BSLBATT
identity text, the BSLBATT device marker frame, or enough core BMS-CAN frames
without a conflicting identity.

The list output for each discovered device is a single XML element:

```xml
<device serial="ABC123" version="12.45" description="BSLBATT BMS" id="0xB021" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
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
- the Victron-assigned Product ID is `0xB021`.

## Firmware Update Flow

Firmware paths must be absolute. Firmware filenames have no prefix restriction,
including extensionless VRM cache names. ZIP packages must contain exactly one
`.bin`, `.fw` or `.img` payload. ZIP CRC is checked before CAN opens. Firmware must be nonempty and at most 65535 × 128 bytes.
For example: `-c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin`.

`bslbatt-tool.py` embeds the update protocol and supports single-file deployment:

1. Send actual byte size via extended ID `0x4610`; wait for `0x4621/A1`
   negotiating 128-byte blocks.
2. Send each block number (`0x4630`, little endian), sixteen 8-byte data frames
   (`0x4650`), then CRC16/Modbus (`0x4670`); wait for `0x4681/A2`.
   Pad the tail with `FF`. Block CRC covers all 128 data bytes; the size field is zero.
3. Send CRC16 of the actual firmware via `0x4690`; wait for `0x46A1/A3`.
4. Send restart `0x46B0`; wait for `0x46C1/0A` or `0B`.
5. Wait 15 seconds and send the first `0x46D0` status query. Successful sending
   completes the flow with exit code 0; the final device status is **unconfirmed**.

Control frames are zero-padded to 8 bytes. Following typical captured timings,
frame spacing is 2 ms, the initial size-ACK delay is 48 ms, inter-block ACK delay
is 47 ms, and verification/restart delays are 32 ms each. Other stages have a
300-second ACK timeout. Each block is sent exactly once. After sending its block
CRC, wait up to 300 seconds for `0x4681/A2`; a timeout or explicit device error
aborts the upgrade without retransmitting the block. Received frames are logged
in bounded batches so busy CAN traffic cannot overflow the log buffer.

stdout contains XML only: stage messages and progress from 0 through 90 during
transfer, 95 after CRC verification, and 100 after the first status query is sent.
The final message is `Update flow completed; device final status unconfirmed`.
`--can-log` records TX/RX and timing; `--debug` writes diagnostics to stderr.
Log failures do not abort the transfer. These checks do not validate
hardware compatibility or firmware authenticity.

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
| 0 | Listing completed; update flow completed with device final status unconfirmed |
| 1 | General error |
| 2 | CAN init error |
| 3 | CAN communication error |
| 4 | Timeout |
| 5 | Firmware error or incompatible firmware |
| 6 | Argument error |
| 7 | Reserved: device not found |
| 8 | Device memory/update error reported by BMS |
| 9 | Firmware file error |
| 10 | Verification failed |
| 11 | Reserved: verification timeout |

List mode uses codes `0`, `1`, `2`, `3`, and `6`. The current update flow does not
return reserved codes `7` or `11`; ACK timeouts return `4`. Ctrl+C during an update
returns `130`; argparse returns `2` for command-line parsing errors.
