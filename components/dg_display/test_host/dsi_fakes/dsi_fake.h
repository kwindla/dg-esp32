#pragma once
#include <cstddef>
#include <cstdint>
using esp_err_t=int;
inline constexpr esp_err_t ESP_OK=0;
inline constexpr int GPIO_NUM_NC=-1,MIPI_DSI_DPI_CLK_SRC_DEFAULT=0,LCD_COLOR_FMT_RGB565=1,LCD_RGB_ELEMENT_ORDER_RGB=0;
inline constexpr int ESP_CACHE_MSYNC_FLAG_DIR_C2M=1,ESP_CACHE_MSYNC_FLAG_UNALIGNED=2;
using esp_lcd_dsi_bus_handle_t=void*;
using esp_lcd_panel_io_handle_t=void*;
using esp_lcd_panel_handle_t=void*;
using esp_ldo_channel_handle_t=void*;
struct esp_ldo_channel_config_t {int chan_id=0,voltage_mv=0;};
struct esp_lcd_dsi_bus_config_t {int bus_id=0,num_data_lanes=0,lane_bit_rate_mbps=0;};
struct esp_lcd_dbi_io_config_t {int lcd_cmd_bits=0,lcd_param_bits=0;};
struct esp_lcd_dpi_panel_config_t {
    int virtual_channel=0,dpi_clk_src=0,dpi_clock_freq_mhz=0,in_color_format=0,out_color_format=0,num_fbs=0;
    struct Timing {int h_size=0,v_size=0,hsync_back_porch=0,hsync_pulse_width=0,hsync_front_porch=0,
        vsync_back_porch=0,vsync_pulse_width=0,vsync_front_porch=0;} video_timing;
};
struct jd9365_lcd_init_cmd_t {int command;const void* data;size_t bytes;unsigned delay_ms;};
struct jd9365_vendor_config_t {
    const jd9365_lcd_init_cmd_t* init_cmds=nullptr;size_t init_cmds_size=0;
    struct Mipi {esp_lcd_dsi_bus_handle_t dsi_bus=nullptr;esp_lcd_dpi_panel_config_t* dpi_config=nullptr;int lane_num=0;} mipi_config;
};
struct esp_lcd_panel_dev_config_t {int bits_per_pixel=0,rgb_ele_order=0,reset_gpio_num=0;void* vendor_config=nullptr;};
struct esp_lcd_dpi_panel_event_data_t {};
using FrameCallback=bool (*)(esp_lcd_panel_handle_t,esp_lcd_dpi_panel_event_data_t*,void*);
struct esp_lcd_dpi_panel_event_callbacks_t {FrameCallback on_frame_buf_complete=nullptr;};
esp_err_t esp_ldo_acquire_channel(const esp_ldo_channel_config_t*,esp_ldo_channel_handle_t*);
esp_err_t esp_lcd_new_dsi_bus(const esp_lcd_dsi_bus_config_t*,esp_lcd_dsi_bus_handle_t*);
esp_err_t esp_lcd_new_panel_io_dbi(esp_lcd_dsi_bus_handle_t,const esp_lcd_dbi_io_config_t*,esp_lcd_panel_io_handle_t*);
esp_err_t esp_lcd_new_panel_jd9365(esp_lcd_panel_io_handle_t,const esp_lcd_panel_dev_config_t*,esp_lcd_panel_handle_t*);
esp_err_t esp_lcd_panel_reset(esp_lcd_panel_handle_t);
esp_err_t esp_lcd_panel_init(esp_lcd_panel_handle_t);
esp_err_t esp_lcd_dpi_panel_get_frame_buffer(esp_lcd_panel_handle_t,int,void**);
esp_err_t esp_lcd_dpi_panel_register_event_callbacks(esp_lcd_panel_handle_t,const esp_lcd_dpi_panel_event_callbacks_t*,void*);
esp_err_t esp_lcd_panel_disp_on_off(esp_lcd_panel_handle_t,bool);
esp_err_t esp_cache_msync(void*,size_t,int);
bool esp_ptr_external_ram(const void*);
int64_t esp_timer_get_time();
