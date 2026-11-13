#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_board/power.h"
#include "dg_power_decode.h"
#include "sdkconfig.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
namespace dg_board {
namespace {
portMUX_TYPE guard=portMUX_INITIALIZER_UNLOCKED;
PowerSnapshot latest{};
}
void power::init() {
}
PowerSnapshot power::read() {portENTER_CRITICAL(&guard);const auto copy=latest;portEXIT_CRITICAL(&guard);return copy;}
}
