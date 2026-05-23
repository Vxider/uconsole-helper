# XIAO MCU Telemetry Firmware

Firmware for Seeed XIAO nRF52840 Sense / Sense Plus.

It reads the onboard IMU, PDM microphone peak level, and an optional VEML7700
ambient light sensor on I2C address `0x10`, then emits compact JSON events over
USB Serial. It does not stream continuously by default; it reports only state
changes, safety events, a slow heartbeat, or an explicit host request.

Example:

```json
{"event":"pose_changed","pose":"face_up","motion":"still","g":1.0042,"delta":0.0120,"mic":{"ready":true,"peak":42,"recent_peak":false},"light":{"ready":true,"valid":true,"raw":121,"lux":6.97},"accel":{"x":0.0100,"y":0.0200,"z":1.0040}}
```

The GTK MCU page parses the `accel.x/y/z` JSON object and displays
`light.lux` when the VEML7700 is present.

## Events

- `ready`: emitted once after IMU init succeeds.
- `pose_changed`: stable orientation changed for at least 700 ms.
- `motion_started`: acceleration delta stayed above motion threshold.
- `motion_stopped`: movement settled back down. A recent microphone peak or
  put-down impact shortens the required stable IMU window.
- `pose_calibrated`: response after `calibrate pose`; the current orientation is
  saved to internal flash and used as the resting orientation after reboot.
- `freefall`: total acceleration dropped below 0.35 g.
- `impact`: total acceleration exceeded 1.85 g.
- `heartbeat`: slow 30 s health/status update.
- `requested`: response to `status`, `sample`, or `?`.

## Poses

- `face_up`: Z axis up.
- `face_down`: Z axis down.
- `left_edge` / `right_edge`: X axis dominant.
- `top_edge` / `bottom_edge`: Y axis dominant.
- `tilted`: no axis is dominant.

## Commands

Send one line over USB Serial:

```text
status
sample
?
calibrate pose
stream on
stream off
help
```

`stream on` is for debugging only; normal GUI use should stay event-driven.

## Build

Install PlatformIO, then run:

```bash
cd firmware/xiao-mcu-telemetry
pio run
```

This project follows Seeed's PlatformIO setup for XIAO nRF52840:
`platform = https://github.com/Seeed-Studio/platform-seeedboards.git` and
`board = seeed-xiao-afruitnrf52-nrf52840-sense-plus`.

## Flash

Upload with:

```bash
./scripts/flash-uf2.sh
```

The script calls `pio run -t upload`, which lets PlatformIO handle the XIAO
1200-baud reset and uploads the generated
`.pio/build/xiao_nrf52840_sense/firmware.zip` through the Adafruit nRF52
bootloader. If PlatformIO cannot find the upload port, pass `--bootloader` to
request bootloader mode before upload, or double-click reset and retry with
`--no-bootloader`.

After flashing, `/dev/ttyACM0` should print a `ready` JSON event at 115200 baud
and then only print when posture/motion changes or when the host sends `status`.
