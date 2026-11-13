# EchoEar v1.2 display sources
Factory image strings identify `echoear_core_board_v1_2_ctm`. Espressif now calls this board ESP-VoCat v1.2.
Pinned esp-brookesia commit: `90ba55a446d62ccfb9ae0671760583011301b45a`.
- [board_devices.yaml](https://raw.githubusercontent.com/espressif/esp-brookesia/90ba55a446d62ccfb9ae0671760583011301b45a/hal/brookesia_hal_boards/boards/espressif/esp_vocat_board_v1_2/board_devices.yaml), SHA-256 `abee7445df13871d1deccf8c58c4d539301194be22fbe3cfc6b57a5e89f10868`.
- [board_peripherals.yaml](https://raw.githubusercontent.com/espressif/esp-brookesia/90ba55a446d62ccfb9ae0671760583011301b45a/hal/brookesia_hal_boards/boards/espressif/esp_vocat_board_v1_2/board_peripherals.yaml), SHA-256 `393d41b75ceb08377b311118c6f7463f642c005134f2d559eb7f0a2900a5ff63`.
- [setup_device.c](https://raw.githubusercontent.com/espressif/esp-brookesia/90ba55a446d62ccfb9ae0671760583011301b45a/hal/brookesia_hal_boards/boards/espressif/esp_vocat_board_v1_2/setup_device.c), SHA-256 `7900245d748fce959b2d669113b52d39799c26821ffdad9d74c0b2fd9dd96f36`.

ST77916: 360×360 RGB565, SPI2 SCLK18, D0=46, D1=13, D2=11, D3=12, CS14. QSPI encodes commands in-band, so no DC GPIO is driven. LCD/SD power GPIO9 active-low; reset GPIO47 active-high (inverting transistor); backlight GPIO44 active-high.

[Espressif schematic](https://dl.espressif.com/AE/esp-dev-kits/ESP-VoCat_SCH_V1_2.pdf) SHA-256 `2899d14123a3be2ee7a28110370e629a7e1173e9629c6d67f87a379e044951be`: page 2 connects TE to GPIO8. Observe GPIO8 and enable command 0x35 after the board init table. Never use GPIO18 for TE on EchoEar.

Registry driver pinned to `espressif/esp_lcd_st77916 ==2.0.2`. [Registry component source (`repository_info.commit_sha`)](https://github.com/espressif/esp-iot-solution/blob/91aeb7fb41e8a3e76aeb21371f9f83711c74cf3f/components/display/lcd/esp_lcd_st77916/esp_lcd_st77916_spi.c): arbitrary inclusive start/exclusive end pixel coordinates (no SPD2010 x%4 restriction). Panel alignment is 1. The inherited display policy expands DMA regions to 8 pixels, retaining 16-byte row/transfer lengths; this is not an S3 hardware requirement in the current unencrypted, non-ECC, unweighted-arbitration configuration (IDF TX external alignment is 1). Legacy SPD2010 stays x%4 with even stripe heights for 8-byte rows.

Init command bytes and delays copied unchanged into `echoear_panel.c`; license retained in `ECHOEAR-LICENSE`. The board table ends with sleep-out, then 120 ms delay; display-on is called separately.

Fable A1 review items 6–8 (2026-10-14): `app_main` now calls
`dg_board::early_board_init` before other application initialization. It
preloads GPIO9 low, enables that output alone, and waits `pdMS_TO_TICKS(10)+1`
(the extra tick guarantees at least 10 ms despite tick phase). Only then does
it configure GPIO44 off, explicitly clearing the ROM U0RXD pull-up with
`GPIO_FLOATING`. Display initialization later pulses the active-high GPIO47
reset for at least 10 ms, releases it for at least 120 ms, then configures SPI2
and sends the unchanged vendor sequence, including SLPOUT + 120 ms. No touch
pins are configured by this component. Backlight uses the board LEDC helper
with duty1023; GPIO44 pull clearing precedes LEDC ownership. GPIO9/47/44
have one owner in `dg_board`, adopted on master in3df48a9. The early hook
remains the first `app_main` action; rail power is not deferred to `disp init`.

The data cache is currently 32-byte lines. Staging uses 64-byte alignment and
two 10,240-byte halves, both whole multiples of 32 and 64. A compile-time check
rejects a cache line incompatible with this allocation and split. The inherited TX
length policy is separately 16 bytes: IDF v6.1 `spi_common.c` uses the TX
GDMA alignment (1 byte in this configuration) and writes cache back with `ESP_CACHE_MSYNC_FLAG_UNALIGNED`.
Rounded cache writeback therefore stays within the allocated half; it need
not change a 360-pixel RGB565 row into a 64-byte-multiple transfer. The existing
priority guard, bounce/error counters, internal hash scratch and flash gate
remain required. The census now logs cache line, half size, detected PSRAM and
`heap_caps_get_total_size(MALLOC_CAP_SPIRAM)` for the next hardware run.

Saved `B/wire-2/serial.log` contains no `Virtual address not enough for PSRAM`
warning. Its exact ELF (`4c0eed371cc7d37d685978a9dddfb6415b3ddbd12c353eb5d64a967bd3bb31d2`)
accounts for all 16 MiB: 26 instruction pages + 8 rodata pages at 65,536 bytes
consume 2,228,224 bytes; the logged 14,208 KiB main heap pool contributes the
remaining 14,548,992 bytes. IDF also returns the rodata alignment gap to heap.
Thus a heap below 16 MiB here reflects XIP, not truncation. The machine audit
is `B/review-A1-psram-audit.json` outside git. B's HIL entry now rejects the
truncation warning. Recheck the merged image's boot log and total heap because
XIP size changes with the firmware. Display store access still uses partition
reads, rejects active flash mmap before initialization/TX, and adds no 4 MiB
asset mapping. No mapping-budget or cache-line Kconfig change is needed for
this measured image; the shared defaults/comment and future 64-byte throughput
comparison remain board-owner work.

B1 integration: the standalone Kconfig stub is removed; the master board choice
and EchoEar codec driver are mandatory. Earlier panel/cat/wire logs used legacy
audio pins and cannot qualify this integrated firmware. Startup now verifies
`GDMA.channel[lcd_tx_id].out.pri.tx_pri == 1`, reports `tx_pri` in
`DISPLAY_R4_CRYPTO` and PERF, and prints the channel/register at LCD init.
Failure refuses `disp init` before creating/enabling the panel, and retry needs
a reboot; the console and voice can continue without a display. This is a
display-initialization error, not a fatal firmware/session error.
The HIL monitor treats it as a failed graphics run. All Y bounds now use
`PanelHeight`; the even-Y policy/status is enabled only for the legacy panel.

Rebased HIL at source `acc5835` (master `c7bec53`, ELF
`f763a4402041c540965ccf9b62b43f70e3d9fc75119b10b3f402d089d206efea`):
panel eight-frame software CRC passes; GDMA channel 1 reads `tx_pri=1`;
no GPIO conflicts or PSRAM truncation. Cat idle 29.354 fps, active persistence
29.682 fps; display monitor clean and memory floors met. Evidence outside git:
`B/panel-integrated-1`, `B/cat-integrated-1`. Their pass scope is display/SPI
and the post-init cat window, not error-free board startup.

The subsequent live voice+cat attempt (`B/voice-cat-integrated-2`) fails the
strict raw-error monitor before graphics setup, on `I2C_If: Fail to write to
dev 30` during ES8311 startup. The same boot error exists in both preceding
runs; gain/volume readback succeeds afterwards. Here `30` is the codec
control interface's hexadecimal 8-bit address (7-bit 0x18). The pinned
esp_codec_dev ES8311 open routine documents an occasional first-write
failure and duplicates the GPIO_REG44 write, but the current error log
omits the failed register, so that cause is not proven. The audio owner must
resolve/instrument this before claiming a clean live merge check. No monitor
exception or acoustic qualification was introduced; GRAPHICS WORKING remains
unclaimed. The new GPIO sequence's source ordering and functional bring-up
are checked; no oscilloscope timing measurement is claimed.

Current integration update: master698d644 fixes and verifies the initial
ES8311 REG44 NACK with one bounded retry; master73dc840 derives final REG44
readback from the reference configuration. Fresh B2 panel/cat/wire and live
logs have clean strict startup. The earlier f763a440 I2C failure above is
retained historical evidence. B2's current qualification blocker is full-turn
transport loss/uplink tails, documented in docs/echoear.md and the B report;
all previous pre-isolation acoustic results remain potentially contaminated.
