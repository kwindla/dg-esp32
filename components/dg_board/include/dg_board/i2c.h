#pragma once
#include "driver/i2c_master.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
namespace dg_board::i2c {
// Board-lifetime I2C handle; initialize before retrieving it.
esp_err_t init();
i2c_master_bus_handle_t bus();
// EchoEar/Waveshare: expander/RMW mutex only; other transactions use IDF
// locking. P4: also serializes GPIO27 reset with Goodix probes/reads;
// codec transactions retain IDF transaction locking.
SemaphoreHandle_t mutex();
esp_err_t init_expander();
}
