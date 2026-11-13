# JD9365 product backend

`panel_init.h` is copied without byte/order/delay changes from
`spikes/p4_panel/main/panel_init.h`: the 197 single-byte commands of the
800×800 / 3.4-inch branch of Waveshare BSP 3.0.1. Source repository:
https://github.com/waveshareteam/Waveshare-ESP32-components at
`bfeb6e6d5737178cdde78b630c8118074da0a657`,
`bsp/esp32_p4_wifi6_touch_lcd_xc/esp32_p4_wifi6_touch_lcd_xc.c`, lines 44–261.
Original source SHA256:
`cdd3bc3032b17bf6485fba112dd8e7abb20cc5b864ed0615235d85701b4a69dd`.
Apache-2.0 license retained in [LICENSE](LICENSE).

Power, lanes, RGB format and porches in `backend.cpp` follow the same BSP and
the spike. Product differences: one driver-owned PSRAM framebuffer; 40 MHz
instead of the BSP's 80 MHz (Facts B: lowest completed ≥50 Hz point,
54.112554 Hz); reset delegated to dg_board before the first touch probe,
without an extra
DCS SWRESET. The RMW/reset mutex is not a bus-wide codec/touch barrier; 13b
must coordinate subsequent resets and re-probe Goodix. No BSP or LVGL component
is linked. Single-buffer tearing is unverified;
Facts B observed an underrun even at 40 MHz.

`dsi_read.cpp` / `bounded_wait.h` retain the spike's bounded replacement for
IDF 6.1's unbounded DCS read. All phases share 500 ms and block one tick per
poll. The HAL has a void API, so timeout/short response fails closed via abort;
the coordinator restores factory on acquisition failure/abort. Both `--wrap`
and `--undefined` root the wrapper; a requested 38 MHz hung before this fix.
