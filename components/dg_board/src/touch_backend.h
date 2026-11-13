#pragma once
#include "esp_err.h"
#include <cstdint>
namespace dg_board::detail {
esp_err_t touch_hardware_init();
uint32_t touch_interrupt_count();
bool touch_to_logical(uint16_t& x,uint16_t& y)noexcept;
}
