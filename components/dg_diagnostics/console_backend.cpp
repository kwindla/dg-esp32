#include "console_backend.h"
#include "dg_board/console.h"
namespace dg_console {
const Backend& backend() {
    if constexpr(dg_board::console_kind==dg_board::ConsoleKind::Uart)return uart_backend;
    else return usb_serial_jtag_backend;
}
}
