// Run the actual product backend with recording driver/board APIs. In JD9365
// 2.0.2, panel_reset with GPIO NC sends DCS 0x01; catch any such extra call.
#include "dsi_fake.h"
#include "dg_display/present.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_diagnostics.h"
#include <gtest/gtest.h>
#include <array>
#include <string>
#include <vector>
namespace {
std::vector<std::string> steps;
std::array<uint16_t,800*800> framebuffer{};
esp_lcd_dpi_panel_config_t dpi;
bool fail=false;unsigned sync_calls=0;
}
namespace dg_board {
esp_err_t backlight::set(unsigned pct){steps.push_back("brightness:"+std::to_string(pct));return ESP_OK;}
esp_err_t lcd::reset(){steps.push_back("hardware-reset-27");return ESP_OK;}
}
esp_err_t esp_ldo_acquire_channel(const esp_ldo_channel_config_t* config,esp_ldo_channel_handle_t* handle){
    EXPECT_EQ(config->chan_id,3);EXPECT_EQ(config->voltage_mv,2500);steps.push_back("ldo");*handle=&dpi;return ESP_OK;
}
esp_err_t esp_lcd_new_dsi_bus(const esp_lcd_dsi_bus_config_t* config,esp_lcd_dsi_bus_handle_t* handle){
    EXPECT_EQ(config->bus_id,0);EXPECT_EQ(config->num_data_lanes,2);EXPECT_EQ(config->lane_bit_rate_mbps,1500);
    steps.push_back("dsi");*handle=&dpi;return ESP_OK;
}
esp_err_t esp_lcd_new_panel_io_dbi(esp_lcd_dsi_bus_handle_t,const esp_lcd_dbi_io_config_t* config,esp_lcd_panel_io_handle_t* handle){
    EXPECT_EQ(config->lcd_cmd_bits,8);EXPECT_EQ(config->lcd_param_bits,8);steps.push_back("dbi");*handle=&dpi;return ESP_OK;
}
esp_err_t esp_lcd_new_panel_jd9365(esp_lcd_panel_io_handle_t,const esp_lcd_panel_dev_config_t* config,esp_lcd_panel_handle_t* handle){
    EXPECT_EQ(config->reset_gpio_num,GPIO_NUM_NC);EXPECT_EQ(config->bits_per_pixel,16);
    auto* vendor=static_cast<jd9365_vendor_config_t*>(config->vendor_config);EXPECT_EQ(vendor->mipi_config.lane_num,2);
    EXPECT_EQ(vendor->init_cmds_size,197u);dpi=*vendor->mipi_config.dpi_config;
    steps.push_back("new-panel");*handle=&dpi;return ESP_OK;
}
esp_err_t esp_lcd_panel_reset(esp_lcd_panel_handle_t){steps.push_back("DCS-SWRESET:01");return ESP_OK;}
esp_err_t esp_lcd_panel_init(esp_lcd_panel_handle_t){steps.push_back("panel-init");return fail?-1:ESP_OK;}
esp_err_t esp_lcd_dpi_panel_get_frame_buffer(esp_lcd_panel_handle_t,int count,void** out){
    EXPECT_EQ(count,1);steps.push_back("framebuffer");*out=framebuffer.data();return ESP_OK;
}
esp_err_t esp_lcd_dpi_panel_register_event_callbacks(esp_lcd_panel_handle_t,const esp_lcd_dpi_panel_event_callbacks_t* callbacks,void* context){
    EXPECT_NE(callbacks->on_frame_buf_complete,nullptr);EXPECT_NE(context,nullptr);steps.push_back("callbacks");return ESP_OK;
}
esp_err_t esp_lcd_panel_disp_on_off(esp_lcd_panel_handle_t,bool on){EXPECT_TRUE(on);steps.push_back("display-on");return ESP_OK;}
esp_err_t esp_cache_msync(void* start,size_t bytes,int flags){++sync_calls;EXPECT_EQ(start,framebuffer.data()+800);EXPECT_EQ(bytes,1600u);EXPECT_EQ(flags,ESP_CACHE_MSYNC_FLAG_DIR_C2M|ESP_CACHE_MSYNC_FLAG_UNALIGNED);return ESP_OK;}
bool esp_ptr_external_ram(const void* p){return p==framebuffer.data();}
int64_t esp_timer_get_time(){return 100;}
extern "C" void dg_deferred_log(const char*,...){}
#ifndef DG_TEST_DSI_FAILURE
TEST(Jd9365Init,HardwareResetOnlyPreservesMeasuredSequenceAndDpiTiming){
    auto& panel=dg_display::panel_backend();ASSERT_EQ(panel.init(),dg_display::PresentError::None);
    const std::vector<std::string> expected={"brightness:0","ldo","dsi","dbi","new-panel","hardware-reset-27",
        "panel-init","framebuffer","callbacks","display-on","brightness:50"};
    EXPECT_EQ(steps,expected); // Any extra panel_reset/DCS SWRESET fails this trace.
    const uint16_t pixel=0x3412;
    const auto id=panel.submit({1,1,2,2},&pixel,1);
    EXPECT_TRUE(bool(id));EXPECT_EQ(panel.committed(id),dg_display::PresentError::None);
    EXPECT_EQ(sync_calls,1u);EXPECT_EQ(framebuffer[801],0x1234);
    EXPECT_EQ(dpi.num_fbs,1);EXPECT_EQ(dpi.dpi_clock_freq_mhz,40);
    EXPECT_EQ(dpi.video_timing.h_size,800);EXPECT_EQ(dpi.video_timing.v_size,800);
    EXPECT_EQ(dpi.video_timing.hsync_back_porch,20);EXPECT_EQ(dpi.video_timing.hsync_pulse_width,20);EXPECT_EQ(dpi.video_timing.hsync_front_porch,40);
    EXPECT_EQ(dpi.video_timing.vsync_back_porch,12);EXPECT_EQ(dpi.video_timing.vsync_pulse_width,4);EXPECT_EQ(dpi.video_timing.vsync_front_porch,24);
    ASSERT_EQ(panel.init(),dg_display::PresentError::None);EXPECT_EQ(steps,expected);
}

#else
TEST(Jd9365Init,FailureStaysDarkAndDoesNotRepeatHardware){
    fail=true;
    auto& panel=dg_display::panel_backend();
    EXPECT_EQ(panel.init(),dg_display::PresentError::Io);
    const auto attempted=steps;
    EXPECT_EQ(panel.init(),dg_display::PresentError::Io);EXPECT_EQ(steps,attempted);
    ASSERT_FALSE(steps.empty());EXPECT_EQ(steps.front(),"brightness:0");
    for(const auto& step:steps)EXPECT_NE(step,"brightness:50");
    EXPECT_EQ(steps.back(),"panel-init");
}

#endif
