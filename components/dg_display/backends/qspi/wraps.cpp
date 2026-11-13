#include "dg_display.h"
#include "dg_display/panel_config.h"
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
namespace {
std::atomic<unsigned> psram_direct{0}, psram_bounce{0}, psram_internal_bounce{0}, dma_tx_errors{0};
}
extern "C" esp_err_t __real_spicommon_dma_setup_priv_buffer(spi_host_device_t, uint32_t*, uint32_t, bool, bool, bool, uint32_t**);
extern "C" esp_err_t __wrap_spicommon_dma_setup_priv_buffer(spi_host_device_t host, uint32_t* buffer, uint32_t len, bool tx, bool external, bool allocate, uint32_t** result) {
    auto error=__real_spicommon_dma_setup_priv_buffer(host,buffer,len,tx,external,allocate,result);
    if(error==ESP_OK && tx && external && buffer && len && esp_ptr_external_ram(buffer)) {
        if(*result==buffer) ++psram_direct;
        else {++psram_bounce;if(!esp_ptr_external_ram(*result)) ++psram_internal_bounce;}
    }
    return error;
}
namespace {
std::atomic<bool> capture_lcd_device{false};
spi_device_handle_t lcd_device=nullptr;
transaction_cb_t lcd_pre=nullptr,lcd_post=nullptr;
void observed_pre(spi_transaction_t* t){if(t->flags & SPI_TRANS_DMA_USE_PSRAM)dg_display::runtime_dma_start();if(lcd_pre)lcd_pre(t);}
void observed_post(spi_transaction_t* t){if(t->flags & SPI_TRANS_DMA_USE_PSRAM)dg_display::runtime_dma_done();if(lcd_post)lcd_post(t);}
std::atomic<unsigned> lcd_error_generation{0};
std::atomic<unsigned> requested_clock_hz{40000000},actual_clock_khz{0};
constexpr TickType_t LcdWait=pdMS_TO_TICKS(2000);
}
extern "C" esp_err_t __real_spi_bus_add_device(spi_host_device_t, const spi_device_interface_config_t*, spi_device_handle_t*);
extern "C" esp_err_t __wrap_spi_bus_add_device(spi_host_device_t host,const spi_device_interface_config_t* cfg,spi_device_handle_t* device) {
    auto observed=*cfg;
    if(host==SPI2_HOST && capture_lcd_device.load()){lcd_pre=cfg->pre_cb;lcd_post=cfg->post_cb;observed.pre_cb=observed_pre;observed.post_cb=observed_post;}
    auto error=__real_spi_bus_add_device(host,&observed,device);
    if(error==ESP_OK && host==SPI2_HOST && capture_lcd_device.load())lcd_device=*device;
    return error;
}
extern "C" esp_err_t __real_spi_device_get_trans_result(spi_device_handle_t, spi_transaction_t**, TickType_t);
extern "C" esp_err_t __wrap_spi_device_get_trans_result(spi_device_handle_t device, spi_transaction_t** result, TickType_t wait) {
    if(result)*result=nullptr;
    auto error=__real_spi_device_get_trans_result(device,result,device==lcd_device?std::min(wait,LcdWait):wait);
    if(result && *result) {
        const unsigned flags=(*result)->flags & (SPI_TRANS_DMA_TX_FAIL|SPI_TRANS_DMA_RX_FAIL);
        if(flags) {
            ++dma_tx_errors;
            if(device==lcd_device) {
                dg_display::runtime_dma_error(flags);
                ++lcd_error_generation;
                // Only a consumed LCD DMA-failed result is normalized. esp_lcd
                // must decrement its inflight count before the descriptor is reused.
                if(error==ESP_ERR_INVALID_STATE){(*result)->flags &= ~flags;error=ESP_OK;}
            }
        }
    }
    if(device==lcd_device && error==ESP_ERR_TIMEOUT)dg_display::runtime_dma_dead();
    return error;
}
extern "C" esp_err_t __real_spi_device_queue_trans(spi_device_handle_t,spi_transaction_t*,TickType_t);
extern "C" esp_err_t __wrap_spi_device_queue_trans(spi_device_handle_t device,spi_transaction_t* transaction,TickType_t wait) {
    auto error=__real_spi_device_queue_trans(device,transaction,device==lcd_device?std::min(wait,LcdWait):wait);
    if(device==lcd_device && error==ESP_ERR_TIMEOUT)dg_display::runtime_dma_dead();
    return error;
}
extern "C" esp_err_t __real_spi_device_polling_transmit(spi_device_handle_t,spi_transaction_t*);
extern "C" esp_err_t __wrap_spi_device_polling_transmit(spi_device_handle_t device,spi_transaction_t* transaction) {
    if(device!=lcd_device)return __real_spi_device_polling_transmit(device,transaction);
    // v6.1 accepts only portMAX_DELAY in polling_start/acquire_bus. The
    // independent timer supervises those waits. polling_end supports a bound.
    auto error=spi_device_polling_start(device,transaction,portMAX_DELAY);
    if(error!=ESP_OK)return error;
    error=spi_device_polling_end(device,LcdWait);
    if(error==ESP_ERR_TIMEOUT){
        dg_display::runtime_dma_dead();
        // Keep the command's stack and any driver-owned buffer alive until reboot.
        for(;;)vTaskSuspend(nullptr);
    }
    return error;
}
extern "C" esp_err_t __real_esp_flash_read(esp_flash_t*,void*,uint32_t,uint32_t);
extern "C" esp_err_t __wrap_esp_flash_read(esp_flash_t* chip,void* bytes,uint32_t address,uint32_t length){auto token=dg_display::runtime_spi1_begin(0,address,length);auto error=__real_esp_flash_read(chip,bytes,address,length);dg_display::runtime_spi1_end(token,error);return error;}
extern "C" esp_err_t __real_esp_flash_write(esp_flash_t*,const void*,uint32_t,uint32_t);
extern "C" esp_err_t __wrap_esp_flash_write(esp_flash_t* chip,const void* bytes,uint32_t address,uint32_t length){auto token=dg_display::runtime_spi1_begin(1,address,length);auto error=__real_esp_flash_write(chip,bytes,address,length);dg_display::runtime_spi1_end(token,error);return error;}
extern "C" esp_err_t __real_esp_flash_erase_region(esp_flash_t*,uint32_t,uint32_t);
extern "C" esp_err_t __wrap_esp_flash_erase_region(esp_flash_t* chip,uint32_t address,uint32_t length){auto token=dg_display::runtime_spi1_begin(2,address,length);auto error=__real_esp_flash_erase_region(chip,address,length);dg_display::runtime_spi1_end(token,error);return error;}
namespace dg_display {
unsigned dma_bounces() noexcept{return psram_bounce.load();}
unsigned dma_errors() noexcept{return dma_tx_errors.load();}
unsigned dma_generation() noexcept{return lcd_error_generation.load();}
void dma_capture_device(bool capture) noexcept{capture_lcd_device=capture;}
spi_device_handle_t qspi_device() noexcept{return lcd_device;}
}
