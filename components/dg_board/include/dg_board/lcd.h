#pragma once
#include "esp_err.h"
namespace dg_board {
namespace lcd {esp_err_t reset();}
// Shared non-IRAM interrupt service for touch and LCD TE; never reinstall.
esp_err_t init_gpio_isr_service();
}
