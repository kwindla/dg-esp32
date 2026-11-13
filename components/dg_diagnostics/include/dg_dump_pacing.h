#pragma once
#include <cstddef>
#include <cstdint>
namespace dg_console {
// UART 8N1: R=baud/10, budget=0.8*R. The selected P4 2Mbaud rate
// was measured at 199,998 B/s; baud/10 differs by <0.002%.
constexpr uint64_t dump_delay_us(bool uart,unsigned baud,size_t bytes,unsigned usb_ms=8) {
    return uart?(uint64_t(bytes)*12500000+baud-1)/baud:uint64_t(usb_ms)*1000;
}
}
