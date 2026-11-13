#pragma once
#include "esp_err.h"
typedef void* esp_lcd_panel_io_handle_t;
#ifdef __cplusplus
extern "C" {
#endif
esp_err_t esp_lcd_panel_io_tx_param(esp_lcd_panel_io_handle_t,int,const void*,size_t);
esp_err_t esp_lcd_panel_io_tx_color(esp_lcd_panel_io_handle_t,int,const void*,size_t);
#ifdef __cplusplus
}
#endif
