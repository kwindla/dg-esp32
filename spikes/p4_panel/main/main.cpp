// JD9365 800x800 timing probe; constants from Waveshare BSP 3.0.1.
#include "console.h"
#include "measure.h"
#include "panel_init.h"
#include "driver/ledc.h"
#include "esp_cache.h"
#include "esp_lcd_jd9365.h"
#include "esp_lcd_mipi_dsi.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_io.h"
#include "esp_ldo_regulator.h"
#include "esp_memory_utils.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <atomic>
#include <cstring>
namespace {
using measure::emit;
constexpr unsigned width=800,height=800,capacity=4096;
esp_lcd_dsi_bus_handle_t bus;
esp_lcd_panel_io_handle_t io;
esp_lcd_panel_handle_t panel;
uint16_t* fb[2];
uint32_t vsync_times[capacity],frame_times[capacity];
std::atomic<unsigned> nv{0},nf{0},frame_epoch{0};std::atomic<bool> measuring{false};
TaskHandle_t draw_task;
bool vsync(esp_lcd_panel_handle_t,esp_lcd_dpi_panel_event_data_t*,void*) {
    if(measuring) {auto index=nv.fetch_add(1);if(index<capacity)vsync_times[index]=esp_timer_get_time();}
    return false;
}
bool complete(esp_lcd_panel_handle_t,esp_lcd_dpi_panel_event_data_t*,void*) {
    ++frame_epoch;
    if(measuring) {auto index=nf.fetch_add(1);if(index<capacity)frame_times[index]=esp_timer_get_time();}
    BaseType_t wake=pdFALSE;
    if(draw_task)vTaskNotifyGiveFromISR(draw_task,&wake);
    return wake==pdTRUE;
}
void brightness(unsigned value) {
    ESP_ERROR_CHECK(ledc_set_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0,1023*value/100));
    ESP_ERROR_CHECK(ledc_update_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0));
}
void setup(unsigned mhz) {
    brightness(0);
    if(panel) {
        ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel,false));
        ESP_ERROR_CHECK(esp_lcd_panel_io_tx_param(io,0x10,nullptr,0)); // DCS sleep-in
        vTaskDelay(pdMS_TO_TICKS(120));
        ESP_ERROR_CHECK(esp_lcd_panel_del(panel));panel=nullptr;
    }
    esp_lcd_dpi_panel_config_t dpi{};dpi.virtual_channel=0;dpi.dpi_clk_src=MIPI_DSI_DPI_CLK_SRC_DEFAULT;
    dpi.dpi_clock_freq_mhz=mhz;dpi.in_color_format=LCD_COLOR_FMT_RGB565;dpi.out_color_format=LCD_COLOR_FMT_RGB565;dpi.num_fbs=2;
    dpi.video_timing.h_size=width;dpi.video_timing.v_size=height;
    dpi.video_timing.hsync_back_porch=20;dpi.video_timing.hsync_pulse_width=20;dpi.video_timing.hsync_front_porch=40;
    dpi.video_timing.vsync_back_porch=12;dpi.video_timing.vsync_pulse_width=4;dpi.video_timing.vsync_front_porch=24;
    // All copied BSP commands have exactly one data byte. Store the data in
    // stable storage for the driver's reset/init lifetime, not stack temporaries.
    static jd9365_lcd_init_cmd_t commands[sizeof panel_init/sizeof panel_init[0]];
    for(unsigned i=0;i<sizeof panel_init/sizeof panel_init[0];++i)
        commands[i]={panel_init[i].command,&panel_init[i].value,1,panel_init[i].delay_ms};
    jd9365_vendor_config_t vendor{};vendor.init_cmds=commands;vendor.init_cmds_size=sizeof commands/sizeof commands[0];
    vendor.mipi_config.dsi_bus=bus;vendor.mipi_config.dpi_config=&dpi;vendor.mipi_config.lane_num=2;
    esp_lcd_panel_dev_config_t dev{};dev.bits_per_pixel=16;dev.rgb_ele_order=LCD_RGB_ELEMENT_ORDER_RGB;
    // GPIO27 also resets GT911; its address can relatch (0x5D or 0x14).
    dev.reset_gpio_num=GPIO_NUM_27;dev.vendor_config=&vendor;
    ESP_ERROR_CHECK(esp_lcd_new_panel_jd9365(io,&dev,&panel));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel));ESP_ERROR_CHECK(esp_lcd_panel_init(panel));
    ESP_ERROR_CHECK(esp_lcd_dpi_panel_get_frame_buffer(panel,2,reinterpret_cast<void**>(&fb[0]),reinterpret_cast<void**>(&fb[1])));
    ESP_ERROR_CHECK(esp_ptr_external_ram(fb[0]) && esp_ptr_external_ram(fb[1])?ESP_OK:ESP_FAIL);
    esp_lcd_dpi_panel_event_callbacks_t cb{};cb.on_frame_buf_complete=complete;cb.on_vsync=vsync;
    ESP_ERROR_CHECK(esp_lcd_dpi_panel_register_event_callbacks(panel,&cb,nullptr));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel,true));brightness(50);
}
void draw(uint16_t* pixels,unsigned moving) {
    constexpr uint16_t colors[]={0xffff,0xffe0,0x07ff,0x07e0,0xf81f,0xf800,0x001f,0};
    for(unsigned y=0;y<height;++y)for(unsigned x=0;x<width;++x)
        pixels[y*width+x]=(x==0 || y==0 || x==width-1 || y==height-1)?0xffff:
            ((x>=moving && x<moving+8)?0xffff:colors[x/(width/8)]);
    ESP_ERROR_CHECK(esp_cache_msync(pixels,width*height*2,ESP_CACHE_MSYNC_FLAG_DIR_C2M));
}
void run(unsigned mhz,unsigned seconds) {
    setup(mhz);vTaskDelay(pdMS_TO_TICKS(500));
    nv=0;nf=0;measuring=true;
    auto began=esp_timer_get_time();unsigned frame=0,index=1;
    while(esp_timer_get_time()-began<int64_t(seconds)*1000000) {
        draw(fb[index],1+frame++%790);
        ulTaskNotifyTake(pdTRUE,0);
        ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(panel,0,0,width,height,fb[index]));
        // A callback can race the draw request. Two completions after the
        // request guarantee the old scanout buffer is free even if an in-flight
        // IRQ had already selected it. This limits animation to half refresh;
        // refresh timing is measured independently by both callbacks.
        auto epoch=frame_epoch.load();
        while(frame_epoch.load()-epoch<2)
            ESP_ERROR_CHECK(ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(1000))?ESP_OK:ESP_ERR_TIMEOUT);
        index^=1;
    }
    measuring=false;vTaskDelay(pdMS_TO_TICKS(50));unsigned v=nv.load(),f=nf.load();
    emit("{\"kind\":\"panel_begin\",\"clock_mhz\":%u,\"seconds\":%u,\"width\":800,\"height\":800,\"h_total\":880,\"v_total\":840,\"psram\":true}",mhz,seconds);
    for(unsigned i=0;i<v && i<capacity;++i)emit("{\"kind\":\"panel_vsync\",\"seq\":%u,\"us32\":%lu}",i,(unsigned long)vsync_times[i]);
    for(unsigned i=0;i<f && i<capacity;++i)emit("{\"kind\":\"panel_frame\",\"seq\":%u,\"us32\":%lu}",i,(unsigned long)frame_times[i]);
    emit("{\"kind\":\"panel_end\",\"vsync\":%u,\"frame_complete\":%u,\"overflow\":%s}",v,f,v>capacity || f>capacity?"true":"false");
}
void command(const char* line) {
    unsigned mhz,seconds,baud;char extra;
    if(sscanf(line,"panel %u %u %c",&mhz,&seconds,&extra)==2 && mhz>=30 && mhz<=80 && seconds>=2 && seconds<=30)run(mhz,seconds);
    else if(sscanf(line,"baud %u %c",&baud,&extra)==1)probe::baud(baud);
    else if(!strcmp(line,"uptime"))probe::print("UPTIME us=%lld\n",(long long)esp_timer_get_time());
    else probe::print("ERR panel arguments\n");
    probe::print("OK\n");
}
}
extern "C" void app_main() {
    probe::console_init();draw_task=xTaskGetCurrentTaskHandle();probe::print("P4_PANEL boot\n");
    ledc_timer_config_t timer{};timer.speed_mode=LEDC_LOW_SPEED_MODE;timer.duty_resolution=LEDC_TIMER_10_BIT;
    timer.timer_num=LEDC_TIMER_1;timer.freq_hz=5000;timer.clk_cfg=LEDC_AUTO_CLK;ESP_ERROR_CHECK(ledc_timer_config(&timer));
    ledc_channel_config_t channel{};channel.gpio_num=26;channel.speed_mode=LEDC_LOW_SPEED_MODE;channel.channel=LEDC_CHANNEL_0;
    channel.timer_sel=LEDC_TIMER_1;channel.flags.output_invert=1;ESP_ERROR_CHECK(ledc_channel_config(&channel));
    static esp_ldo_channel_handle_t ldo;esp_ldo_channel_config_t power{};power.chan_id=3;power.voltage_mv=2500;
    ESP_ERROR_CHECK(esp_ldo_acquire_channel(&power,&ldo));
    esp_lcd_dsi_bus_config_t dsi{};dsi.bus_id=0;dsi.num_data_lanes=2;dsi.lane_bit_rate_mbps=1500;
    ESP_ERROR_CHECK(esp_lcd_new_dsi_bus(&dsi,&bus));esp_lcd_dbi_io_config_t dbi{};dbi.lcd_cmd_bits=8;dbi.lcd_param_bits=8;
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_dbi(bus,&dbi,&io));
    probe::print("P4_PANEL READY\n");char line[128];size_t used=0;
    for(;;) {char data[128];int n=probe::read(data,sizeof data);for(int i=0;i<n;++i) {
        char c=data[i];if(c=='\r')continue;
        if(c=='\n') {line[used]=0;if(used)command(line);used=0;}
        else if(used+1<sizeof line)line[used++]=c;
    }}
}
