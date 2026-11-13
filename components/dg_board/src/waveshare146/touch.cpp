#include "dg_board/touch.h"
#include "../touch_backend.h"
namespace dg_board {
esp_err_t touch::read_raw(TouchPoint&){return ESP_ERR_NOT_SUPPORTED;}
namespace detail {
esp_err_t touch_hardware_init(){return ESP_ERR_NOT_SUPPORTED;}
uint32_t touch_interrupt_count(){return 0;}
}
}
