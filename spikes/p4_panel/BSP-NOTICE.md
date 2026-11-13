# Copied BSP constants and init commands

Source: Waveshare `esp32_p4_wifi6_touch_lcd_xc` BSP **3.0.1**, repository
`https://github.com/waveshareteam/Waveshare-ESP32-components`, commit
`bfeb6e6d5737178cdde78b630c8118074da0a657`, path
`bsp/esp32_p4_wifi6_touch_lcd_xc` (package `idf_component.yml`).
The original Apache-2.0 license is retained in [LICENSE](LICENSE).
No BSP component or LVGL dependency is linked.

`main/panel_init.h` contains only the 197 single-byte commands in the
`CONFIG_BSP_LCD_TYPE_800_800_3_4_INCH` branch of the BSP C source (lines 44–261),
with the same order and delays. The byte/delay constants are unchanged; the
representation changes from C compound literals to a C++ constexpr table.
The alternative 720x720 panel's sequence is excluded.

`main/main.cpp` copies the same BSP's timing/power/pins: 800x800 RGB565,
porches H back/pulse/front 20/20/40 and V 12/4/24; two DSI lanes at 1500 Mbps;
PHY LDO channel 3 at 2500 mV; LCD reset GPIO27; 5 kHz, 10-bit LEDC backlight
on GPIO26 with output inversion. Pixel clock starts at the BSP's 80 MHz and
is swept by the coordinator. Two driver-owned PSRAM framebuffers are used.
A one-pixel white border, colour bars and moving white bar are local test art.

Local cache source hashes (before transformation):

- `esp32_p4_wifi6_touch_lcd_xc.c`: SHA256 `cdd3bc3032b17bf6485fba112dd8e7abb20cc5b864ed0615235d85701b4a69dd`.
- `include/bsp/display.h`: SHA256 `600542a7545dd6a52982170b21babbd9755de7f0ad72db9f1c99485500583c71`.
- `include/bsp/esp32_p4_wifi6_touch_lcd_xc.h`: SHA256 `9ab473e79e91c2c0bdcf8f998bf16e2d5a29d0043520779b9cd450b763a08807`.
