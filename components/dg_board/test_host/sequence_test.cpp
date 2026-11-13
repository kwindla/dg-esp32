#include <gtest/gtest.h>
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "sdkconfig.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "freertos/task.h"
#include <array>
#include <string>
#include <vector>
namespace {
std::vector<std::string> events;
int bus_token,device_token;unsigned bus_creates=0,device_adds=0,channels=0;
std::array<uint8_t,4> registers{0,0xa5,0,0xff};
int read_error=ESP_OK,timer_error=ESP_OK;
ledc_timer_config_t timer_config{};ledc_channel_config_t channel_config{};
void event(const char* name,uint64_t a,uint64_t b){events.push_back(std::string(name)+":"+std::to_string(a)+":"+std::to_string(b));}
}
esp_err_t gpio_set_level(int pin,int value){event("level",pin,value);return ESP_OK;}
esp_err_t gpio_set_pull_mode(int pin,int value){event("pull",pin,value);return ESP_OK;}
esp_err_t gpio_config(const gpio_config_t* c){event("gpio",c->pin_bit_mask,c->mode);return ESP_OK;}
void vTaskDelay(TickType_t ticks){event("delay",ticks,0);}
SemaphoreHandle_t xSemaphoreCreateMutexStatic(StaticSemaphore_t* storage){event("mutex",0,0);return storage;}
int xSemaphoreTake(SemaphoreHandle_t mutex,TickType_t ticks){EXPECT_EQ(mutex,dg_board::i2c::mutex());EXPECT_EQ(ticks,portMAX_DELAY);event("take",0,0);return 1;}
int xSemaphoreGive(SemaphoreHandle_t mutex){EXPECT_EQ(mutex,dg_board::i2c::mutex());event("give",0,0);return 1;}
esp_err_t i2c_new_master_bus(const i2c_master_bus_config_t* c,i2c_master_bus_handle_t* handle){
    EXPECT_EQ(c->i2c_port,0);EXPECT_EQ(c->glitch_ignore_cnt,7);EXPECT_TRUE(c->flags.enable_internal_pullup);
#if CONFIG_DG_BOARD_ECHOEAR
    EXPECT_EQ(c->sda_io_num,2);EXPECT_EQ(c->scl_io_num,1);
#elif CONFIG_DG_BOARD_P4XC
    EXPECT_EQ(c->sda_io_num,7);EXPECT_EQ(c->scl_io_num,8);
#else
    EXPECT_EQ(c->sda_io_num,11);EXPECT_EQ(c->scl_io_num,10);
#endif
    ++bus_creates;*handle=&bus_token;return ESP_OK;
}
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t bus,const i2c_device_config_t* c,i2c_master_dev_handle_t* handle){
    EXPECT_EQ(bus,&bus_token);EXPECT_EQ(c->device_address,0x20);EXPECT_EQ(c->scl_speed_hz,400000u);
    ++device_adds;*handle=&device_token;return ESP_OK;
}
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t device,const uint8_t* reg,size_t n,uint8_t* data,size_t size,int timeout){
    EXPECT_EQ(device,&device_token);EXPECT_EQ(n,1u);EXPECT_EQ(size,1u);EXPECT_EQ(timeout,100);event("read",*reg,0);
    if(read_error==ESP_OK)*data=registers[*reg];
    return read_error;
}
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t device,const uint8_t* bytes,size_t size,int timeout){
    EXPECT_EQ(device,&device_token);EXPECT_EQ(size,2u);EXPECT_EQ(timeout,100);registers[bytes[0]]=bytes[1];event("write",bytes[0],bytes[1]);return ESP_OK;
}
esp_err_t ledc_timer_config(const ledc_timer_config_t* c){timer_config=*c;return timer_error;}
esp_err_t ledc_channel_config(const ledc_channel_config_t* c){channel_config=*c;++channels;return ESP_OK;}
TEST(BoardSequence,PreservesLegacyIoOrderLevelsDelaysAndFailures){
    EXPECT_EQ(dg_board::i2c::bus(),nullptr);EXPECT_EQ(dg_board::i2c::mutex(),nullptr);
    dg_board::early_board_init();
#if CONFIG_DG_BOARD_ECHOEAR
    // Pre-step-5 board.cpp:36-46: low latch BEFORE GPIO output; >=10ms.
    EXPECT_EQ(events,(std::vector<std::string>{"level:9:0","gpio:512:3","delay:11:0","level:44:0","pull:44:0","gpio:17592186044416:3"}));
#elif CONFIG_DG_BOARD_P4XC
    EXPECT_EQ(events,(std::vector<std::string>{"level:26:1","gpio:67108864:3"}));
#else
    EXPECT_TRUE(events.empty()); // Original early-board action was EchoEar-only.
#endif
    events.clear();EXPECT_EQ(dg_board::i2c::init(),ESP_OK);EXPECT_EQ(dg_board::i2c::init(),ESP_OK);EXPECT_EQ(bus_creates,1u);
    EXPECT_NE(dg_board::i2c::mutex(),nullptr);events.clear();
    EXPECT_EQ(dg_board::i2c::init_expander(),ESP_OK);
#if CONFIG_DG_BOARD_ECHOEAR || CONFIG_DG_BOARD_P4XC
    EXPECT_EQ(device_adds,0u);EXPECT_TRUE(events.empty());
#else
    // Pre-step-5 board.cpp:71-79: preserve every bit except EXIO2 (bit1).
    EXPECT_EQ(device_adds,1u);EXPECT_EQ(registers[1],0xa7);EXPECT_EQ(registers[3],0xfd);
    EXPECT_EQ(events,(std::vector<std::string>{"take:0:0","read:1:0","write:1:167","give:0:0","take:0:0","read:3:0","write:3:253","give:0:0"}));
#endif
    events.clear();EXPECT_EQ(dg_board::lcd::reset(),ESP_OK);
#if CONFIG_DG_BOARD_ECHOEAR
    // Pre-step-5 board.cpp:84-92: reset low/config/high, 11ms, low,121ms.
    EXPECT_EQ(events,(std::vector<std::string>{"level:47:0","gpio:140737488355328:3","level:47:1","delay:11:0","level:47:0","delay:121:0"}));
#elif CONFIG_DG_BOARD_P4XC
    EXPECT_EQ(events,(std::vector<std::string>{"take:0:0","level:27:0","gpio:134217728:3","delay:11:0","level:27:1","delay:121:0","give:0:0"}));
#else
    // Pre-step-5 board.cpp:94-99: serialized bit1 low/high,10/120ms.
    EXPECT_EQ(events,(std::vector<std::string>{"take:0:0","read:1:0","write:1:165","give:0:0","delay:10:0","take:0:0","read:1:0","write:1:167","give:0:0","delay:120:0"}));
    events.clear();read_error=ESP_ERR_TIMEOUT;EXPECT_EQ(dg_board::lcd::reset(),ESP_ERR_TIMEOUT);
    EXPECT_EQ(events,(std::vector<std::string>{"take:0:0","read:1:0","give:0:0"}));read_error=ESP_OK;
#endif
    EXPECT_EQ(dg_board::backlight::init(),ESP_OK);EXPECT_EQ(channels,1u);
    EXPECT_EQ(timer_config.duty_resolution,10);EXPECT_EQ(timer_config.timer_num,0);EXPECT_EQ(timer_config.speed_mode,LEDC_LOW_SPEED_MODE);
#if CONFIG_DG_BOARD_ECHOEAR
    // Pre-step-5 board.cpp:107-108,117,124.
    EXPECT_EQ(timer_config.freq_hz,2000);EXPECT_EQ(channel_config.gpio_num,44);EXPECT_EQ(channel_config.duty,1023u);
#elif CONFIG_DG_BOARD_P4XC
    EXPECT_EQ(timer_config.freq_hz,5000);EXPECT_EQ(channel_config.gpio_num,26);EXPECT_EQ(channel_config.duty,1023u);
#else
    // Pre-step-5 board.cpp:110,119,122.
    EXPECT_EQ(timer_config.freq_hz,5000);EXPECT_EQ(channel_config.gpio_num,5);EXPECT_EQ(channel_config.duty,512u);
#endif
    timer_error=ESP_ERR_TIMEOUT;EXPECT_EQ(dg_board::backlight::init(),ESP_ERR_TIMEOUT);EXPECT_EQ(channels,1u);
}
esp_err_t ledc_set_duty(int mode,int channel,unsigned duty){event("duty",duty,channel);EXPECT_EQ(mode,LEDC_LOW_SPEED_MODE);return read_error;}
esp_err_t ledc_update_duty(int mode,int channel){event("update",mode,channel);return timer_error;}
TEST(BoardSequence,BacklightPercentageAndDriverErrors){
    read_error=timer_error=ESP_OK;events.clear();
    EXPECT_EQ(dg_board::backlight::set(101),ESP_ERR_INVALID_ARG);EXPECT_TRUE(events.empty());
    EXPECT_EQ(dg_board::backlight::set(35),ESP_OK);
    EXPECT_EQ(events,(std::vector<std::string>{"duty:"+std::to_string((DG_TEST_BOARD==DG_TEST_P4XC)?664:358)+":0","update:0:0"}));
    events.clear();read_error=ESP_ERR_TIMEOUT;EXPECT_EQ(dg_board::backlight::set(50),ESP_ERR_TIMEOUT);
    EXPECT_EQ(events,(std::vector<std::string>{"duty:511:0"}));
    read_error=ESP_OK;timer_error=ESP_ERR_TIMEOUT;EXPECT_EQ(dg_board::backlight::set(0),ESP_ERR_TIMEOUT);
    timer_error=ESP_OK;
}
