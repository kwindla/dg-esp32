#include "../qspi/panel_init.h"
#include "dg_board/backlight.h"
#include "driver/gpio.h"
#include "esp_lcd_panel_vendor.h"
extern "C" esp_err_t dg_new_echoear_panel(esp_lcd_panel_io_handle_t,esp_lcd_panel_dev_config_t*,esp_lcd_panel_handle_t*);
namespace dg_display {
esp_err_t qspi_init_panel(esp_lcd_panel_io_handle_t io,esp_lcd_panel_handle_t* panel,PanelCensus census){
    esp_lcd_panel_dev_config_t config{};config.reset_gpio_num=GPIO_NUM_NC;
    config.rgb_ele_order=LCD_RGB_ELEMENT_ORDER_RGB;config.bits_per_pixel=16;
    auto error=panel_step(census,"st77916-object",[&]{return dg_new_echoear_panel(io,&config,panel);});if(error!=ESP_OK)return error;
    error=panel_step(census,"st77916-init",[&]{return esp_lcd_panel_init(*panel);});if(error!=ESP_OK)return error;
    error=panel_step(census,"st77916-on",[&]{return esp_lcd_panel_disp_on_off(*panel,true);});if(error!=ESP_OK)return error;
    // Enable TE output; observer uses GPIO8, never QSPI clock GPIO18.
    const uint8_t te_mode=0;ESP_ERROR_CHECK(esp_lcd_panel_io_tx_param(io,0x02003500,&te_mode,1));
    return panel_step(census,"ledc-backlight",[]{return dg_board::backlight::init();});
}
}
