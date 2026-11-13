#pragma once
#include "esp_err.h"
#include <cstdint>
using gpio_num_t=int;
constexpr int GPIO_MODE_INPUT=1,GPIO_INTR_NEGEDGE=2,GPIO_NUM_10=10;
struct gpio_config_t {uint64_t pin_bit_mask;int mode,intr_type;};
esp_err_t gpio_config(const gpio_config_t*);
esp_err_t gpio_install_isr_service(int);
esp_err_t gpio_isr_handler_add(int,void(*)(void*),void*);

constexpr int GPIO_MODE_OUTPUT=3,GPIO_NUM_9=9,GPIO_NUM_44=44,GPIO_NUM_47=47,GPIO_NUM_2=2,GPIO_NUM_1=1,GPIO_NUM_11=11,GPIO_FLOATING=0;
esp_err_t gpio_set_level(int,int);
esp_err_t gpio_set_pull_mode(int,int);
