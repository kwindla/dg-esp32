#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_board/power.h"
#include "dg_power_decode.h"
#include "sdkconfig.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
namespace dg_board {
namespace {
portMUX_TYPE guard=portMUX_INITIALIZER_UNLOCKED;
PowerSnapshot latest{};
TaskHandle_t worker;
void run(void*) {
    i2c_master_dev_handle_t gauge=nullptr;
    for(;;) {
        PowerSnapshot sample=power::read();sample.sampled_us=esp_timer_get_time();
        esp_err_t error=ESP_ERR_NOT_FOUND;
        if(!gauge && i2c_master_probe(i2c::bus(),0x55,10)==ESP_OK) {
            i2c_device_config_t cfg{};cfg.dev_addr_length=I2C_ADDR_BIT_LEN_7;
            cfg.device_address=0x55;cfg.scl_speed_hz=100000;
            error=i2c_master_bus_add_device(i2c::bus(),&cfg,&gauge);
        }
        if(gauge) {
            uint16_t values[4]{};const uint8_t regs[]={0x06,0x08,0x14,0x2c};error=ESP_OK;
            for(unsigned i=0;i<4 && error==ESP_OK;++i) {
                uint8_t bytes[2]{};
                error=i2c_master_transmit_receive(gauge,&regs[i],1,bytes,2,10);
                values[i]=gauge_word(bytes[0],bytes[1]);
            }
            if(error==ESP_OK && (values[3]>100 || values[1]<1000 || values[1]>6000))error=ESP_ERR_INVALID_RESPONSE;
            if(error==ESP_OK) {
                sample.temperature_centideg_c=gauge_temperature_centideg(values[0]);
                sample.voltage_mv=values[1];sample.current_ma=gauge_current(values[2]);sample.soc_percent=values[3];
            }
        }
        sample.error=error;sample.valid=error==ESP_OK;++sample.samples;if(error!=ESP_OK)++sample.errors;
        portENTER_CRITICAL(&guard);latest=sample;portEXIT_CRITICAL(&guard);
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
}
void power::init() {
    if(!worker && i2c::bus()) {
        if(xTaskCreatePinnedToCoreWithCaps(run,"power-gauge",3072,nullptr,1,&worker,0,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT)!=pdPASS)abort();
    }
}
PowerSnapshot power::read() {portENTER_CRITICAL(&guard);const auto copy=latest;portEXIT_CRITICAL(&guard);return copy;}
}
