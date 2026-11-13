#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_board/audio_hw.h"
#include "sdkconfig.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
namespace dg_board {
namespace {
i2c_master_bus_handle_t shared_bus;
}
void early_board_init() {
    // Q1 has no gate pull resistor. Latch low before enabling its output,
    // before any other board peripheral (in particular SPI2 or touch I2C).
    ESP_ERROR_CHECK(gpio_set_level(gpio_num_t(audio_hw().board_rail.gpio),audio_hw().board_rail.active_high));
    gpio_config_t power{};power.pin_bit_mask=1ull<<audio_hw().board_rail.gpio;
    power.mode=GPIO_MODE_OUTPUT;ESP_ERROR_CHECK(gpio_config(&power));
    // One extra tick guarantees >=10 ms even when called just before a tick.
    vTaskDelay(pdMS_TO_TICKS(10)+1);
    // U0RXD's ROM pull-up fights the backlight gate's external pulldown.
    // Establish off before handing the pin to GPIO, then LEDC after panel init.
    ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_44,0));
    ESP_ERROR_CHECK(gpio_set_pull_mode(GPIO_NUM_44,GPIO_FLOATING));
    gpio_config_t backlight{};backlight.pin_bit_mask=1ull<<44;
    backlight.mode=GPIO_MODE_OUTPUT;ESP_ERROR_CHECK(gpio_config(&backlight));
}
i2c_master_bus_handle_t i2c::bus() { return shared_bus; }
SemaphoreHandle_t i2c::mutex() {
    if(!shared_bus)return nullptr;
    static StaticSemaphore_t storage;
    static SemaphoreHandle_t mutex=xSemaphoreCreateMutexStatic(&storage);
    return mutex;
}
esp_err_t i2c::init() {
    if(shared_bus) return ESP_OK;
    i2c_master_bus_config_t cfg{};
    cfg.i2c_port=I2C_NUM_0;
    cfg.sda_io_num=GPIO_NUM_2; cfg.scl_io_num=GPIO_NUM_1;
    cfg.clk_source=I2C_CLK_SRC_DEFAULT; cfg.glitch_ignore_cnt=7;
    cfg.flags.enable_internal_pullup=true;
    return i2c_new_master_bus(&cfg,&shared_bus);
}
esp_err_t i2c::init_expander() {
    // Rail ownership is exclusively early_board_init, called first by app_main.
    return ESP_OK;
}
esp_err_t lcd::reset() {
    ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_47,0));
    gpio_config_t reset{};reset.pin_bit_mask=1ull<<47;
    reset.mode=GPIO_MODE_OUTPUT;ESP_ERROR_CHECK(gpio_config(&reset));
    auto result=gpio_set_level(GPIO_NUM_47, 1);
    if(result!=ESP_OK) return result;
    vTaskDelay(pdMS_TO_TICKS(10)+1);
    result=gpio_set_level(GPIO_NUM_47, 0);
    vTaskDelay(pdMS_TO_TICKS(120)+1);
    return result;
}
esp_err_t backlight::init() {
    ledc_timer_config_t timer{};
    timer.speed_mode=LEDC_LOW_SPEED_MODE; timer.duty_resolution=LEDC_TIMER_10_BIT;
    timer.timer_num=LEDC_TIMER_0;
    ESP_ERROR_CHECK(gpio_set_pull_mode(GPIO_NUM_44,GPIO_FLOATING));
    timer.freq_hz=2000;
     timer.clk_cfg=LEDC_AUTO_CLK;
    auto result=ledc_timer_config(&timer);
    if(result!=ESP_OK) return result;
    ledc_channel_config_t channel{};
    channel.gpio_num=44;
     channel.speed_mode=LEDC_LOW_SPEED_MODE;
    channel.channel=LEDC_CHANNEL_0; channel.timer_sel=LEDC_TIMER_0; channel.duty=512;
    channel.duty=1023; // Preserve the qualified bring-up brightness.
    return ledc_channel_config(&channel);
}
esp_err_t backlight::set(unsigned pct) {
    if(pct>100)return ESP_ERR_INVALID_ARG;
    auto error=ledc_set_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0,pct*1023/100);
    return error==ESP_OK?ledc_update_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0):error;
}

}
