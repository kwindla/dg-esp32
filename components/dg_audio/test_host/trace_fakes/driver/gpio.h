#pragma once
#include "esp_err.h"
using gpio_num_t=int;
constexpr int GPIO_NUM_15=15,GPIO_MODE_OUTPUT=1;
int gpio_set_level(gpio_num_t,unsigned);
int gpio_set_direction(gpio_num_t,int);
