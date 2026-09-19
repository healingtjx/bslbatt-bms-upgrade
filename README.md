# BSLBATT Venus OS Tool

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

The test firmware binaries are not distributed through the public Git
repository. `BSL-V1.245.bin` and `BSL-V1.246.bin` are delivered to Victron as
attachments to the handover email, together with their SHA-256 values:

| File | Size | SHA-256 |
|------|-----:|--------|
| `BSL-V1.245.bin` | 106,020 bytes | `8a7efc20671a1bbc5d6a0cf71c6141c6ba6e3a48a2fb61860b1c3cec6bff3f7e` |
| `BSL-V1.246.bin` | 106,020 bytes | `ce8eb9428d4d91ac487aeabd2e9f93c3150ddf05333639c95ea2d18c9d083049` |

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
python3 bslbatt-tool.py -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin
```

The explicit update flag is optional when `-f` is present, but can be used:

```bash
python3 bslbatt-tool.py -u -c can0 -n 0x0 -f /data/vrmfilescache/P41288V110-41289-1.52T-000.bin
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

The standalone tool embeds the validated `tools/pc_update.py` protocol:

1. Send actual byte size via extended ID `0x4610`; wait for `0x4621/A1`
   negotiating 128-byte blocks.
2. Send each block number (`0x4630`, little endian), sixteen 8-byte data frames
   (`0x4650`), then CRC16/Modbus (`0x4670`); wait for `0x4681/A2`.
   Pad the tail with `FF`. Block CRC covers all 128 data bytes; the size field is zero.
3. Send CRC16 of the actual firmware via `0x4690`; wait for `0x46A1/A3`.
4. Send restart `0x46B0`; wait for `0x46C1/0A` or `0B`.
5. Wait 15 seconds and send the first `0x46D0` status query. Successful sending
   completes the flow with exit code 0; the final device status is **unconfirmed**.

Control frames are zero-padded to 8 bytes. Frame spacing is 3 ms, the initial
size-ACK delay is 48 ms, inter-block ACK delay is 75 ms, and verification/restart
delays are 32 ms each. ACK timeout is 30 seconds. Only one upgrade attempt is
performed; any error or timeout ends it without automatic retry.

stdout contains XML only: stage messages and progress from 0 through 90 during
transfer, 95 after CRC verification, and 100 after the first status query is sent.
The final message is `Update flow completed; device final status unconfirmed`.
`--can-log` records TX/RX and timing; `--debug` writes diagnostics to stderr.
Log failures do not abort the transfer. These checks do not validate
hardware compatibility or firmware authenticity.

## Verified Test Status

The records below describe the historical protocol and are not acceptance evidence for the new protocol.

The formal single-device tests are recorded in `logs/01_*.log` through
`logs/09_*.log`:

- `V1.245 -> V1.246`, `V1.246 -> V1.245`, and repeated upgrades completed
  successfully, and device discovery reported `12.45` or `12.46` afterward.
- A corrupted ZIP member was rejected with exit code `9` before CAN was opened.
- A firmware filename not starting with uppercase `BSL` was rejected with exit
  code `5` before CAN was opened.
- Disconnecting CAN at approximately 52% returned timeout code `4`. The BMS
  remained in its bootloader and required a BMS restart before application CAN
  communication recovered.
- The single-device normal-load 400-second case passed. CAN transfer time was
  `373.688` seconds and the conservative completion bound was approximately
  `385.714` seconds.
- Multi-module behavior was not tested because only one logical device at
  `can0/0x0` was confirmed and the module update mechanism is not defined.

## Historical GX Development Case

The project includes an earlier GX terminal record in `logs/upgrade_case.log`.
It preserves the version strings produced by the historical display rule and
records a development test from version 1.23 to 1.24, which is below the
currently supported minimum remote-update version `V1.245`. It is retained as
historical implementation evidence and is not the formal release acceptance
test. The case was run on a CCGX where `can0` was already up:

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
<device serial="BSLBATT-can0" version="1.23" description="BSLBATT" id="0xB021" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
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
<device serial="BSLBATT-can0" version="1.24" description="BSLBATT" id="0xB021" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
```

Additional observed outputs:

If metadata frames are incomplete, list mode can still report a detected
BSLBATT candidate:

```xml
<device serial="BSLBATT-can0" version="unknown" description="BSLBATT" id="0xB021" type="bslbatt" connection-type="can" connection-id="0x0" connection="socketcan:can0/0x0" updatable="True" />
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

- [x] Complete the agreed code changes and single-device hardware tests.
- [x] Record test cases 01 through 09, with the multi-module case explicitly
  marked blocked/not tested.
- [x] Keep test firmware under the ignored local `doc/firmware/` directory and
  out of the public Git repository.
- [ ] Complete the product metadata and English delivery documents listed in
  `doc/Victron远程固件升级交付任务计划.md`.
- [ ] Review the final commit and create/push the release tag.
