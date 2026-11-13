#pragma once
using gpio_num_t=int;
constexpr gpio_num_t GPIO_NUM_15=15;
int gpio_set_level(gpio_num_t,unsigned);
