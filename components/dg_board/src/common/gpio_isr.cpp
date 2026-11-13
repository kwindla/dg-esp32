#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "driver/gpio.h"
#include <mutex>
namespace dg_board {
esp_err_t init_gpio_isr_service(){
    // Both touch and LCD TE use this single process-wide, non-IRAM service.
    // No unregister path exists; std::call_once also serializes racing callers.
    static std::once_flag once;
    static esp_err_t result=ESP_ERR_INVALID_STATE;
    std::call_once(once,[]{result=gpio_install_isr_service(0);});
    return result;
}
}
