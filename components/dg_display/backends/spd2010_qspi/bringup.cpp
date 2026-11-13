#include "pins.h"
#include "../qspi/panel_init.h"
#include "dg_display.h"
#include "dg_display/panel_config.h"
#include "../qspi/hardware.h"
#include "dg_display/r4_crypto.h"
#include "driver/gpio.h"
#include "dg_display/runtime.h"
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "esp_heap_caps.h"
#include "esp_psram.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_littlefs.h"
#include "esp_http_client.h"
#include "esp_partition.h"
#include "esp_flash.h"
#include "esp_private/flash_mmap.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_memory_utils.h"
#include "esp_rom_crc.h"
#include "driver/spi_master.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <cstring>
#include <cstdio>
#include <stdexcept>
#include <atomic>
#include <algorithm>
namespace dg_display {
namespace {
std::atomic<unsigned> requested_clock_hz{40000000},actual_clock_khz{0};
size_t staging_bytes=StagingBytes;
bool staging_external=true;
constexpr unsigned caps=MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
constexpr unsigned psram=MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
uint8_t* staging;
esp_lcd_panel_io_handle_t io;
esp_lcd_panel_handle_t panel;
TaskHandle_t store_task, display_task;
std::atomic<bool> initialized{false};
std::atomic<bool> init_attempted{false};
unsigned initial_free;
void census(const char* name, unsigned before, esp_err_t result=ESP_OK) {
    const unsigned after=heap_caps_get_free_size(caps);
    ESP_LOGI("lcd-census","object=%s before=%u after=%u delta=%d minimum=%u largest=%u psram=%u result=%d",
        name,before,after,int(before)-int(after),unsigned(heap_caps_get_minimum_free_size(caps)),
        unsigned(heap_caps_get_largest_free_block(caps)),unsigned(heap_caps_get_free_size(psram)),int(result));
}
void checked(const char* name, unsigned before, esp_err_t result) {
    census(name,before,result);
    if(result!=ESP_OK) throw std::runtime_error(name);
}
unsigned free_internal() { return heap_caps_get_free_size(caps); }
bool color_done(esp_lcd_panel_io_handle_t, esp_lcd_panel_io_event_data_t*, void*) {
    if(!dg_display::runtime_color_done())return false;
    BaseType_t wake=pdFALSE;
    if(display_task) vTaskNotifyGiveFromISR(display_task,&wake);
    // esp_lcd v6.1 discards the callback return value.
    if(wake)portYIELD_FROM_ISR();
    return false;
}

}
void census_provision_empty() {
    throw std::runtime_error("storage untouched in r1: provisioning disabled");
}
void census_init(bool external, size_t bytes) {

    if(init_attempted.exchange(true)) throw std::runtime_error("census init already attempted; reboot before retry");
    if(bytes!=StagingBytes || !external) throw std::runtime_error("9a requires 20480-byte direct PSRAM staging");
    staging_bytes=bytes; staging_external=external;
    initial_free=free_internal();
    unsigned before=initial_free;
    staging=static_cast<uint8_t*>(heap_caps_aligned_alloc(StagingAlignment,staging_bytes,
        (external?MALLOC_CAP_SPIRAM:MALLOC_CAP_INTERNAL) | MALLOC_CAP_DMA | MALLOC_CAP_8BIT));
    checked(external?"dma-staging-external":"dma-staging-internal",before,staging?ESP_OK:ESP_ERR_NO_MEM);
    ESP_LOGI("lcd-census","staging bytes=%u external=%d aligned64=%d dma_ext=%d cache_line=%u half_bytes=%u psram_chip=%u psram_heap_total=%u",unsigned(bytes),int(external),int(uintptr_t(staging)%StagingAlignment==0),int(esp_ptr_dma_ext_capable(staging)),DataCacheLine,unsigned(bytes/2),unsigned(esp_psram_get_size()),unsigned(heap_caps_get_total_size(MALLOC_CAP_SPIRAM)));
    before=free_internal(); checked("i2c-bus",before,dg_board::i2c::init());
    before=free_internal(); checked("tca9554",before,dg_board::i2c::init_expander());
    before=free_internal(); checked("lcd-reset",before,dg_board::lcd::reset());
    before=free_internal();
    spi_bus_config_t bus_cfg{};
    bus_cfg.sclk_io_num=LcdClock; bus_cfg.data0_io_num=LcdD0; bus_cfg.data1_io_num=LcdD1;
    bus_cfg.data2_io_num=LcdD2; bus_cfg.data3_io_num=LcdD3;
    bus_cfg.data4_io_num=bus_cfg.data5_io_num=bus_cfg.data6_io_num=bus_cfg.data7_io_num=-1;
    bus_cfg.max_transfer_sz=staging_bytes;
    checked("spi-bus-depth1",before,spi_bus_initialize(SPI2_HOST,&bus_cfg,SPI_DMA_CH_AUTO));
    before=free_internal();
    esp_lcd_panel_io_spi_config_t io_cfg{};
    io_cfg.cs_gpio_num=static_cast<gpio_num_t>(LcdCs); io_cfg.dc_gpio_num=GPIO_NUM_NC; io_cfg.spi_mode=0;
    io_cfg.pclk_hz=requested_clock_hz.load(); io_cfg.lcd_cmd_bits=32; io_cfg.lcd_param_bits=8;
    io_cfg.flags.quad_mode=true; io_cfg.flags.psram_dma_direct=true;
    io_cfg.on_color_trans_done=color_done;
    io_cfg.trans_queue_depth=1;
    dma_capture_device(true);
    auto io_error=esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI2_HOST,&io_cfg,&io);
    dma_capture_device(false);
    checked("lcd-panel-io",before,io_error);
    int actual=0;ESP_ERROR_CHECK(spi_device_get_actual_freq(qspi_device(),&actual));actual_clock_khz=unsigned(actual);
    ESP_LOGI("lcd-clock","requested_hz=%u actual_khz=%u driver_resolution_khz=1",requested_clock_hz.load(),actual_clock_khz.load());
    before=free_internal();
    const auto guard=r4_crypto_health();
    if(!guard.lcd_tx_captured || !guard.priority_applies || guard.last_priority_written!=1 || guard.tx_pri!=1 || guard.coverage_lost)
        throw std::runtime_error("LCD init refused: GDMA priority-1 guard missing; reboot before retry");
    ESP_LOGI("lcd-guard","LCD TX GDMA priority=1 captured=1 applies=%u lcd_tx_id=%d tx_pri=%d",guard.priority_applies,guard.lcd_tx_id,guard.tx_pri);
    const auto panel_error=qspi_init_panel(io,&panel,{free_internal,checked});
    if(panel_error!=ESP_OK)throw std::runtime_error("lcd-panel init");
    ESP_LOGI("lcd-panel","geometry=%dx%d x_align=%d dma_x_align=%d te_gpio=%d",PanelWidth,PanelHeight,PanelXAlign,DmaXAlign,LcdTe);
    before=free_internal();
    if(flash_mmap_remain()) throw std::runtime_error("storage_unavailable: flash mmap");
    runtime_init(panel,io,staging,staging_bytes);
    census("fixed-core",before);
    before=free_internal();
    checked("display-task-psram",before,xTaskCreatePinnedToCoreWithCaps(runtime_display,"display",4096,nullptr,1,&display_task,0,psram)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
    before=free_internal();
    checked("store-task-psram",before,xTaskCreatePinnedToCoreWithCaps(runtime_store,"display-store",8192,nullptr,1,&store_task,0,psram)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
    runtime_wait_ready();
    initialized=true;
    census("lcd-ready",free_internal());
    ESP_LOGI("lcd-census","ready fixed_drop=%d limit=27108 mmap=%u arena=0",int(initial_free)-int(free_internal()),unsigned(flash_mmap_remain()));
}
bool qspi_initialized() noexcept{return initialized.load();}
void census_fetch(const char*) {throw std::runtime_error("use disp fetch JSON with hash and bytes");}
void census_set_clock(unsigned hz){if(init_attempted || (hz!=40000000 && hz!=26666667 && hz!=20000000))throw std::runtime_error("clock requires fresh boot:40000000|26666667|20000000");requested_clock_hz=hz;}
unsigned lcd_requested_hz() noexcept{return requested_clock_hz.load();}
unsigned lcd_actual_khz() noexcept{return actual_clock_khz.load();}
void census_push(unsigned clock,unsigned frames) {if(clock!=lcd_requested_hz() && clock*1000000!=lcd_requested_hz())throw std::runtime_error("qualification must use configured clock");runtime_diagnostic(frames);}
}
