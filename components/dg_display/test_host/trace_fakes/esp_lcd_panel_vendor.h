#pragma once
#include "esp_lcd_panel_interface.h"
#include "esp_lcd_panel_io.h"
#define LCD_RGB_ELEMENT_ORDER_RGB 0
#define LCD_RGB_ELEMENT_ORDER_BGR 1
typedef struct {
    int reset_gpio_num,rgb_ele_order,bits_per_pixel;
    void* vendor_config;
    struct {unsigned reset_active_high:1;} flags;
} esp_lcd_panel_dev_config_t;
