#pragma once
#include "dg_board/board.h"
#include "sdkconfig.h"
namespace dg_board {
// A single compile-time choice is shared by the descriptor and the backend;
// a board/target mismatch must fail compilation, before early log wrappers run.
#if CONFIG_DG_BOARD_P4XC
inline constexpr ConsoleKind console_kind=ConsoleKind::Uart;
#else
inline constexpr ConsoleKind console_kind=ConsoleKind::UsbSerialJtag;
#endif
#if CONFIG_IDF_TARGET_ESP32S3
inline constexpr bool usb_console_target=true;
#else
inline constexpr bool usb_console_target=false;
#endif
static_assert(console_kind!=ConsoleKind::UsbSerialJtag || usb_console_target,
    "USB-JTAG product console requires ESP32-S3");
#if CONFIG_DG_BOARD_P4XC && !CONFIG_ESP_CONSOLE_UART
#error "P4-XC requires the UART console"
#endif
}
