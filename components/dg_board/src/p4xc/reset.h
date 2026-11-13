#pragma once
#include <cstdint>
namespace dg_board::detail {
// Access only while holding i2c::mutex(); every attempted GPIO27 reset
// invalidates Goodix's address and any pending contact, even on failure.
uint32_t lcd_reset_generation();
}
