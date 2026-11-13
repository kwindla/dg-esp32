#include "../qspi/panel_init.h"
#include "dg_board/backlight.h"
#include "driver/gpio.h"
#include "esp_lcd_spd2010.h"
namespace dg_display {
esp_err_t qspi_init_panel(esp_lcd_panel_io_handle_t io,esp_lcd_panel_handle_t* panel,PanelCensus census){
    esp_lcd_panel_dev_config_t config{};config.reset_gpio_num=GPIO_NUM_NC;
    config.rgb_ele_order=LCD_RGB_ELEMENT_ORDER_RGB;config.bits_per_pixel=16;
    spd2010_vendor_config_t vendor{};vendor.flags.use_qspi_interface=true;config.vendor_config=&vendor;
    auto error=panel_step(census,"spd2010-object",[&]{return esp_lcd_new_panel_spd2010(io,&config,panel);});if(error!=ESP_OK)return error;
    error=panel_step(census,"spd2010-init",[&]{return esp_lcd_panel_init(*panel);});if(error!=ESP_OK)return error;
    error=panel_step(census,"spd2010-on",[&]{return esp_lcd_panel_disp_on_off(*panel,true);});if(error!=ESP_OK)return error;
    return panel_step(census,"ledc-backlight",[]{return dg_board::backlight::init();});
}
}
