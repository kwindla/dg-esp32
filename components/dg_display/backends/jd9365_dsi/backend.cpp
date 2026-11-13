// BSP 3.0.1 + measured spike: see BSP-NOTICE.md and docs/p4xc.md Facts B.
#include "framebuffer.h"
#include "panel_init.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_diagnostics.h"
#include "esp_cache.h"
#include "esp_lcd_jd9365.h"
#include "esp_lcd_mipi_dsi.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_ldo_regulator.h"
#include "esp_memory_utils.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_attr.h"
#include <new>
#ifdef ESP_PLATFORM
#include "sdkconfig.h"
#if !CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY
#error JD9365 storage requires external BSS
#endif
#if CONFIG_LCD_DSI_ISR_CACHE_SAFE || CONFIG_DW_GDMA_ISR_IRAM_SAFE
#error PSRAM frame-fence context requires cache-unsafe DSI interrupts
#endif
#endif
namespace dg_display {
namespace {
class Jd9365 final:public DsiFramebuffer {
public:
    NativeInfo info()const noexcept override{return {800,800,true,PixelFormat::Rgb565BE,800,true};}
    PresentError init()override{
        if(attempted_)return initialized_?PresentError::None:PresentError::Io;
        attempted_=true;
        if(set_backlight(0)!=PresentError::None)return PresentError::Io;
        esp_ldo_channel_config_t power{};power.chan_id=3;power.voltage_mv=2500;
        if(esp_ldo_acquire_channel(&power,&ldo_)!=ESP_OK)return PresentError::Io;
        esp_lcd_dsi_bus_config_t bus{};bus.bus_id=0;bus.num_data_lanes=2;bus.lane_bit_rate_mbps=1500;
        if(esp_lcd_new_dsi_bus(&bus,&bus_)!=ESP_OK)return PresentError::Io;
        esp_lcd_dbi_io_config_t dbi{};dbi.lcd_cmd_bits=8;dbi.lcd_param_bits=8;
        if(esp_lcd_new_panel_io_dbi(bus_,&dbi,&io_)!=ESP_OK)return PresentError::Io;
        esp_lcd_dpi_panel_config_t dpi{};dpi.virtual_channel=0;dpi.dpi_clk_src=MIPI_DSI_DPI_CLK_SRC_DEFAULT;
        dpi.dpi_clock_freq_mhz=40;dpi.in_color_format=LCD_COLOR_FMT_RGB565;dpi.out_color_format=LCD_COLOR_FMT_RGB565;dpi.num_fbs=1;
        dpi.video_timing.h_size=800;dpi.video_timing.v_size=800;
        dpi.video_timing.hsync_back_porch=20;dpi.video_timing.hsync_pulse_width=20;dpi.video_timing.hsync_front_porch=40;
        dpi.video_timing.vsync_back_porch=12;dpi.video_timing.vsync_pulse_width=4;dpi.video_timing.vsync_front_porch=24;
        auto& commands=commands_;
        for(unsigned i=0;i<sizeof commands/sizeof commands[0];++i)
            commands[i]={panel_init[i].command,&panel_init[i].value,1,panel_init[i].delay_ms};
        jd9365_vendor_config_t vendor{};vendor.init_cmds=commands;vendor.init_cmds_size=sizeof commands/sizeof commands[0];
        vendor.mipi_config.dsi_bus=bus_;vendor.mipi_config.dpi_config=&dpi;vendor.mipi_config.lane_num=2;
        esp_lcd_panel_dev_config_t dev{};dev.bits_per_pixel=16;dev.rgb_ele_order=LCD_RGB_ELEMENT_ORDER_RGB;
        // GPIO27 reset and touch probes/reads share the board mutex.
        // The touch backend re-probes after any subsequent reset.
        // No panel_reset here: NC would issue an unmeasured DCS SWRESET.
        dev.reset_gpio_num=GPIO_NUM_NC;dev.vendor_config=&vendor;
        if(esp_lcd_new_panel_jd9365(io_,&dev,&panel_)!=ESP_OK || dg_board::lcd::reset()!=ESP_OK ||
           esp_lcd_panel_init(panel_)!=ESP_OK)return PresentError::Io;
        uint16_t* fb=nullptr;
        if(esp_lcd_dpi_panel_get_frame_buffer(panel_,1,reinterpret_cast<void**>(&fb))!=ESP_OK ||
           !esp_ptr_external_ram(fb))return PresentError::Io;
        Hooks hooks;
        hooks.msync=[](void*,void* start,size_t bytes){
            return esp_cache_msync(start,bytes,ESP_CACHE_MSYNC_FLAG_DIR_C2M|ESP_CACHE_MSYNC_FLAG_UNALIGNED)==ESP_OK;
        };
        hooks.clock=[](void*){return uint32_t(esp_timer_get_time());};
        hooks.settled=[](void*,const Event& e){
            // Deterministic 1-in-16 sampling bounds UART/deferred-log load.
            // Completion time comes from the ISR, never owner poll latency.
            if(e.id.value%16==1)dg_deferred_log("P4_SCAN {\"id\":%llu,\"outcome\":\"%s\",\"committed_us32\":%u,\"completed_us32\":%u,\"latency_us\":%u,\"timed\":%s,\"sample_every\":16}",
                (unsigned long long)e.id.value,e.outcome==ScanOutcome::Presented?"presented":"superseded",
                unsigned(e.committed_us),unsigned(e.completed_us),unsigned(uint32_t(e.completed_us-e.committed_us)),e.timed?"true":"false");
        };
        if(attach(fb,info(),hooks)!=PresentError::None)return PresentError::Io;
        esp_lcd_dpi_panel_event_callbacks_t callbacks{};
        callbacks.on_frame_buf_complete=[](esp_lcd_panel_handle_t,esp_lcd_dpi_panel_event_data_t*,void* context){
            static_cast<Jd9365*>(context)->frame_complete(uint32_t(esp_timer_get_time()));return false;
        };
        if(esp_lcd_dpi_panel_register_event_callbacks(panel_,&callbacks,this)!=ESP_OK ||
           esp_lcd_panel_disp_on_off(panel_,true)!=ESP_OK)return PresentError::Io;
        ESP_LOGI("jd9365","800x800 RGB565 LE framebuffer PSRAM=1 DPI=40MHz refresh=54.112554Hz scan_fence=1");
        if(set_backlight(50)!=PresentError::None)return PresentError::Io;
        initialized_=true;
        return PresentError::None;
    }
    PresentError set_backlight(unsigned pct)override{
        if(pct>100)return PresentError::Invalid;
        return dg_board::backlight::set(pct)==ESP_OK?PresentError::None:PresentError::Io;
    }
private:
    jd9365_lcd_init_cmd_t commands_[sizeof panel_init/sizeof panel_init[0]]{};
    bool attempted_=false,initialized_=false;
    esp_ldo_channel_handle_t ldo_=nullptr;esp_lcd_dsi_bus_handle_t bus_=nullptr;
    esp_lcd_panel_io_handle_t io_=nullptr;esp_lcd_panel_handle_t panel_=nullptr;
};
// No initialized vtable/pointer is put in a NOBITS section. The zeroed backing
// store lives in PSRAM from boot; placement construction happens on first use
// after app_main, without malloc or an internal-RAM fallback. CACHE_SAFE=n
// masks the non-IRAM-flagged interrupts during cache-off flash operations.
alignas(Jd9365) EXT_RAM_BSS_ATTR unsigned char panel_storage[sizeof(Jd9365)];
}
PanelBackend& panel_backend()noexcept{
    static auto* const panel=new(panel_storage) Jd9365;
    return *panel;
}
}
