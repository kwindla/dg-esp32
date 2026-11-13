// Pin/order source: spikes/p4_panel/main/main.cpp and BSP-NOTICE.md.
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_board/audio_hw.h"
#include "sdkconfig.h"
#include "reset.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
namespace dg_board {
namespace {
i2c_master_bus_handle_t shared_bus;
uint32_t reset_generation=0;
}
uint32_t detail::lcd_reset_generation(){return reset_generation;}
void early_board_init() {
    // Inverted backlight: latch off before output enable. GPIO54 belongs
    // exclusively to esp_hosted; this backend never resets the C6.
    ESP_ERROR_CHECK(gpio_set_level(gpio_num_t(26),1));
    gpio_config_t pin{};pin.pin_bit_mask=1ull<<26;pin.mode=GPIO_MODE_OUTPUT;
    ESP_ERROR_CHECK(gpio_config(&pin));
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
    cfg.sda_io_num=gpio_num_t(7); cfg.scl_io_num=gpio_num_t(8);
    cfg.clk_source=I2C_CLK_SRC_DEFAULT; cfg.glitch_ignore_cnt=7;
    cfg.flags.enable_internal_pullup=true;
    return i2c_new_master_bus(&cfg,&shared_bus);
}
esp_err_t i2c::init_expander() {
    // No expander or software-controlled board power rail.
    return ESP_OK;
}
esp_err_t lcd::reset() {
    if(!i2c::bus())return ESP_ERR_INVALID_STATE;
    // GPIO27 resets LCD and Goodix together before the first touch probe.
    // Touch probes/reads share this mutex. Codec transactions retain IDF's
    // transaction locking; only LCD/touch use GPIO27. The next touch read
    // re-probes both addresses and discards the pre-reset contact.
    xSemaphoreTake(i2c::mutex(),portMAX_DELAY);
    ++reset_generation;
    auto result=gpio_set_level(gpio_num_t(27),0);
    gpio_config_t pin{};pin.pin_bit_mask=1ull<<27;pin.mode=GPIO_MODE_OUTPUT;
    if(result==ESP_OK)result=gpio_config(&pin);
    if(result==ESP_OK){
        vTaskDelay(pdMS_TO_TICKS(10)+1);
        result=gpio_set_level(gpio_num_t(27),1);
        vTaskDelay(pdMS_TO_TICKS(120)+1);
    }
    xSemaphoreGive(i2c::mutex());return result;
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
    channel.gpio_num=26;
     channel.speed_mode=LEDC_LOW_SPEED_MODE;
    channel.channel=LEDC_CHANNEL_0; channel.timer_sel=LEDC_TIMER_0; channel.duty=1023; // Stay off in the headless image.
    return ledc_channel_config(&channel);
}
esp_err_t backlight::set(unsigned pct) {
    if(pct>100)return ESP_ERR_INVALID_ARG;
    auto error=ledc_set_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0,(100-pct)*1023/100);
    return error==ESP_OK?ledc_update_duty(LEDC_LOW_SPEED_MODE,LEDC_CHANNEL_0):error;
}

}
