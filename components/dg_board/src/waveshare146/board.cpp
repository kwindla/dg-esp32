#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "sdkconfig.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
namespace dg_board {
namespace {
i2c_master_bus_handle_t shared_bus;
i2c_master_dev_handle_t expander;
StaticSemaphore_t mutex_storage;
SemaphoreHandle_t expander_mutex;
esp_err_t read(uint8_t reg, uint8_t& value) {
    return i2c_master_transmit_receive(expander, &reg, 1, &value, 1, 100);
}
esp_err_t write(uint8_t reg, uint8_t value) {
    uint8_t bytes[]{reg,value};
    return i2c_master_transmit(expander, bytes, sizeof(bytes), 100);
}
esp_err_t update(uint8_t reg, uint8_t mask, uint8_t value) {
    xSemaphoreTake(expander_mutex, portMAX_DELAY);
    uint8_t old=0;
    auto result=read(reg,old);
    if(result==ESP_OK) result=write(reg,(old & ~mask) | (value & mask));
    xSemaphoreGive(expander_mutex);
    return result;
}
}
void early_board_init() {
}
i2c_master_bus_handle_t i2c::bus() { return shared_bus; }
SemaphoreHandle_t i2c::mutex() { return expander_mutex; }
esp_err_t i2c::init() {
    if(shared_bus) return ESP_OK;
    expander_mutex=xSemaphoreCreateMutexStatic(&mutex_storage);
    i2c_master_bus_config_t cfg{};
    cfg.i2c_port=I2C_NUM_0;
    cfg.sda_io_num=GPIO_NUM_11; cfg.scl_io_num=GPIO_NUM_10;
    cfg.clk_source=I2C_CLK_SRC_DEFAULT; cfg.glitch_ignore_cnt=7;
    cfg.flags.enable_internal_pullup=true;
    return i2c_new_master_bus(&cfg,&shared_bus);
}
esp_err_t i2c::init_expander() {
    i2c_device_config_t cfg{};
    cfg.dev_addr_length=I2C_ADDR_BIT_LEN_7; cfg.device_address=0x20; cfg.scl_speed_hz=400000;
    auto result=i2c_master_bus_add_device(shared_bus,&cfg,&expander);
    if(result!=ESP_OK) return result;
    // Waveshare EXIO2 is one-based: register bit 1 (not bit 2).
    // Preserve touch/reset/power pins. Establish high before enabling output.
    result=update(1,0x02,0x02);
    if(result==ESP_OK) result=update(3,0x02,0);
    return result;
}
esp_err_t lcd::reset() {
    auto result=update(1,0x02,0);
    if(result!=ESP_OK) return result;
    vTaskDelay(pdMS_TO_TICKS(10));
    result=update(1,0x02,0x02);
    vTaskDelay(pdMS_TO_TICKS(120));
    return result;
}
esp_err_t backlight::init() {
    ledc_timer_config_t timer{};
    timer.speed_mode=LEDC_LOW_SPEED_MODE; timer.duty_resolution=LEDC_TIMER_10_BIT;
    timer.timer_num=LEDC_TIMER_0;
    timer.freq_hz=5000;
     timer.clk_cfg=LEDC_AUTO_CLK;
    auto result=ledc_timer_config(&timer);
    if(result!=ESP_OK) return result;
    ledc_channel_config_t channel{};
    channel.gpio_num=5;
     channel.speed_mode=LEDC_LOW_SPEED_MODE;
    channel.channel=LEDC_CHANNEL_0; channel.timer_sel=LEDC_TIMER_0; channel.duty=512;
    return ledc_channel_config(&channel);
}
esp_err_t backlight::set(unsigned pct) {
    if(pct>100)return ESP_ERR_INVALID_ARG;
    auto error=ledc_set_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0,pct*1023/100);
    return error==ESP_OK?ledc_update_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0):error;
}

}
