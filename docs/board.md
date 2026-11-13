# Waveshare ESP32-S3-Touch-LCD-1.46 display bring-up

Phase 9a uses 40 MHz QSPI and direct PSRAM DMA staging. The clock passes 1,000 full-frame software CRC checks and 1,000 fixed-pose webcam pattern/color observations; this does not establish tear-free scanout or audio synchronization.

The [current Waveshare resources page](https://docs.waveshare.com/ESP32-S3-Touch-LCD-1.46/Resources-And-Documents) links the integrated board's schematic and official demo repository. The old wiki URL returns HTTP 404. Demo source is pinned to commit `fda89ff2ff32eb1bce561901225c6a36fa684237`.

| Signal | Connection |
| --- | --- |
| QSPI D0 / D1 / D2 / D3 | GPIO46 / GPIO45 / GPIO42 / GPIO41 |
| QSPI SCK / CS | GPIO40 / GPIO21, SPI2 |
| TE | GPIO18 |
| Backlight | GPIO5, active high, LEDC |
| Shared I2C SDA / SCL | GPIO11 / GPIO10, I2C0, 400 kHz |
| TCA9554 | address 0x20 |
| LCD reset | EXIO2: **register bit 1**, active low |

Waveshare's expander API numbers EXIO pins from one: `1 << (Pin-1)`. The schematic's TCA9554 P1 connects to LCD_RST. `dg_board` changes only mask 0x02 using serialized read/modify/write, preserving the other pins for touch and power. Reset is held low for at least 10 ms, then high for at least 120 ms before panel initialization. Touch is not initialized in phase 9a.

The panel component is pinned to `espressif/esp_lcd_spd2010==2.0.0~1`. Its default command table matches the pinned Waveshare demo byte-for-byte after ignoring comments and whitespace. It selects RGB565, enables TE with command 0x35, and includes sleep-out/display-on delays. No separate module's wiring or LVGL is used.

The original feasibility census used 20 MHz. The adopted resume configuration uses SPI mode 0 from the board demo, 40 MHz QSPI, queue depth 1 and `max_transfer_sz=20480`. The 20 KiB staging allocation is 64-byte aligned direct PSRAM DMA, split into two owned 10 KiB halves. Runtime assertions reject bounce buffers and DMA errors. The next 80 MHz PSRAM step produced DMA underflow in the retained R1 measurements and is rejected. The 40 MHz qualification evidence is `/home/khkramer/src/evidence/step9a/resume/clock-40MHz-1000/`. [The versioned driver README](https://components.espressif.com/components/espressif/esp_lcd_spd2010/versions/2.0.0~1/readme) requires x start and exclusive x end divisible by four. No vertical alignment or tear-free behavior is claimed until measured.

## Source identities

Downloaded sources and the complete hash manifest are retained under `/home/khkramer/src/evidence/step9a/sources/`.

- [Schematic](https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-1.46/ESP32-S3-Touch-LCD-1.46.pdf): SHA-256 `aa82913fc7099bc066994865b4a6bd17a2eb430a574e1fbbf9af472a91c92e2b`.
- [Panel configuration](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.46/blob/fda89ff2ff32eb1bce561901225c6a36fa684237/example/ESP-IDF-5.3.2/ESP32-S3-Touch-LCD-1.46-Test/main/LCD_Driver/Display_SPD2010.h): SHA-256 `49524921b37de4342b3d1d0f2a94b7cfdf51f099564f7cf5004eddee03a5f14b`.
- [Reset/backlight](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.46/blob/fda89ff2ff32eb1bce561901225c6a36fa684237/example/ESP-IDF-5.3.2/ESP32-S3-Touch-LCD-1.46-Test/main/LCD_Driver/Display_SPD2010.c): SHA-256 `422c80e2e95d71d75bf0c6788dcd88e20c392a6c52086b55b198d72b02ea1248`.
- [Expander numbering](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.46/blob/fda89ff2ff32eb1bce561901225c6a36fa684237/example/ESP-IDF-5.3.2/ESP32-S3-Touch-LCD-1.46-Test/main/EXIO/TCA9554PWR.c): SHA-256 `c3cd5c82f69da3021ff91c36e41a6d13abae24d006e6f6b989799070803ce19e`.
- [I2C pins](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.46/blob/fda89ff2ff32eb1bce561901225c6a36fa684237/example/ESP-IDF-5.3.2/ESP32-S3-Touch-LCD-1.46-Test/main/I2C_Driver/I2C_Driver.h): SHA-256 `366cac5ec11c0f76f3762b89bb289879b1d91e396fbbf2e3aa8a6baf7a767174`.
- [Demo panel driver](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.46/blob/fda89ff2ff32eb1bce561901225c6a36fa684237/example/ESP-IDF-5.3.2/ESP32-S3-Touch-LCD-1.46-Test/main/LCD_Driver/esp_lcd_spd2010/esp_lcd_spd2010.c): SHA-256 `50a37cb6ccdb6d29dec51459b92c0b55f7c4581351e7b567c7d22d77cfe8d721`.

## Restore

Before storage was erased, the complete 16 MiB chip was read twice into
`/home/khkramer/src/evidence/step9a/full-flash-backup/`.
`verification.json` and `SHA256SUMS` record the two lengths/hashes and the
byte-for-byte equality check. Erasure is permitted only after that check passes.
The verified first read is `flash-read-1.bin`, SHA-256
`26f7e8547271dd69a580b9eb02e4dccdb25b4858edae73b1a4b6a9874ee89778`.
It restores the entire pre-erasure chip state, including bootloader, partition
table, current application, NVS and all data regions. With the serial capture
stopped, the exact restore command (esptool 5.5.0) is:

```bash
/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python -m esptool --chip esp32s3 --port /dev/ttyACM0 --baud 460800 --before default-reset --after hard-reset write-flash --flash-mode keep --flash-freq keep --flash-size keep 0x0 /home/khkramer/src/evidence/step9a/full-flash-backup/flash-read-1.bin
```

The complete Waveshare factory demo is pinned for rebuild in
`sources/waveshare-demo-fda89ff2ff32eb1bce561901225c6a36fa684237.tar.gz`,
with archive SHA-256
`0c62fc0eb87e0be1422e6be62c244b5ff2d63516750297e238e070da54c3e843`.
`sources/waveshare-demo-pin.json` records its commit, archive identity and the
36 source files checked against the previously pinned hardware evidence.
The factory project is
`example/ESP-IDF-5.3.2/ESP32-S3-Touch-LCD-1.46-Test`; rebuild it with ESP-IDF
5.3.2 after extracting the pinned archive.

Tracked `git grep` finds the storage partition declaration and design references,
with no production mount/read/write/erase/mmap consumer. The new step-9a display
store is its first project consumer. The unchanged partition table resolves
`storage` to **0x410000, length 0x400000** (end-exclusive 0x810000); only that region
is erased and explicitly provisioned as LittleFS under the user's storage ruling.
Established filesystems are never automatically formatted on mount failure.

## Camera evidence

The completed 1,000-frame 40 MHz qualification uses the original retained camera pose. The user subsequently moved the webcam during campaign continuation. Future optical tests require a fresh full-frame reference, a newly detected lit disc and new calibration geometry/ROIs; the old fixed coordinates are restricted to the historical qualification evidence. Codex does not reposition the camera.
