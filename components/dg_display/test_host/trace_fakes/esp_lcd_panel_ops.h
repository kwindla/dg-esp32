#pragma once
#include "esp_lcd_panel_interface.h"
static inline esp_err_t esp_lcd_panel_init(esp_lcd_panel_handle_t panel){return panel->init(panel);}
static inline esp_err_t esp_lcd_panel_disp_on_off(esp_lcd_panel_handle_t panel,bool on){return panel->disp_on_off(panel,on);}
static inline esp_err_t esp_lcd_panel_del(esp_lcd_panel_handle_t panel){return panel->del(panel);}
