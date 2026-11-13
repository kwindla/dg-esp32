#pragma once
#include "esp_err.h"
#define GPIO_NUM_NC -1
#define GPIO_MODE_OUTPUT 1
typedef struct {unsigned mode;uint64_t pin_bit_mask;} gpio_config_t;
#ifdef __cplusplus
extern "C" {
#endif
esp_err_t gpio_config(const gpio_config_t*);
esp_err_t gpio_reset_pin(int);
esp_err_t gpio_set_level(int,int);
#ifdef __cplusplus
}
#endif
