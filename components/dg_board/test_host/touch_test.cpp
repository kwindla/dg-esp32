#include <gtest/gtest.h>
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_board/touch.h"
#include "driver/gpio.h"
namespace {
int token;bool bus_ready=false;unsigned reads=0,adds=0,installs=0;void(*irq)(void*)=nullptr;
uint8_t points=0;int read_result=ESP_OK;
bool sample(unsigned at,bool down,bool enabled=true){points=down;irq(nullptr);return dg_board::touch::poll(at,enabled);}
}
namespace dg_board {i2c_master_bus_handle_t i2c::bus(){return bus_ready?&token:nullptr;}}
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t bus,const i2c_device_config_t* c,i2c_master_dev_handle_t* d){
    EXPECT_EQ(bus,&token);EXPECT_EQ(c->device_address,0x15);EXPECT_EQ(c->scl_speed_hz,400000u);++adds;*d=&token;return ESP_OK;
}
esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t){return ESP_OK;}
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t d,const uint8_t* reg,size_t n,uint8_t* out,size_t bytes,int timeout){
    EXPECT_EQ(d,&token);EXPECT_EQ(n,1u);EXPECT_EQ(*reg,2);EXPECT_EQ(bytes,5u);EXPECT_EQ(timeout,10);
    ++reads;out[0]=points;out[1]=out[3]=0;out[2]=out[4]=100;return read_result;
}
esp_err_t gpio_config(const gpio_config_t* pin){EXPECT_EQ(pin->pin_bit_mask,1ull<<10);EXPECT_EQ(pin->mode,GPIO_MODE_INPUT);EXPECT_EQ(pin->intr_type,GPIO_INTR_NEGEDGE);return ESP_OK;}
esp_err_t gpio_install_isr_service(int flags){EXPECT_EQ(flags,0);++installs;return ESP_OK;}
esp_err_t gpio_isr_handler_add(int pin,void(*handler)(void*),void*){EXPECT_EQ(pin,10);irq=handler;return ESP_OK;}
TEST(TouchDriver,SharedBusIrqOnlyReadsBoundedAndRejectsAmbiguousContacts){
    EXPECT_EQ(dg_board::touch::init(),ESP_ERR_INVALID_STATE);EXPECT_EQ(adds,0u);
    bus_ready=true;ASSERT_EQ(dg_board::touch::init(),ESP_OK);ASSERT_NE(irq,nullptr);
    EXPECT_EQ(dg_board::touch::init(),ESP_OK);EXPECT_EQ(adds,1u);EXPECT_EQ(reads,0u);
    EXPECT_EQ(dg_board::init_gpio_isr_service(),ESP_OK);EXPECT_EQ(dg_board::init_gpio_isr_service(),ESP_OK);EXPECT_EQ(installs,1u);
    EXPECT_FALSE(dg_board::touch::poll(0,true));EXPECT_EQ(reads,0u);
    EXPECT_FALSE(sample(100,true));EXPECT_FALSE(sample(150,false));
    EXPECT_FALSE(sample(220,true));EXPECT_TRUE(sample(270,false));
    EXPECT_EQ(dg_board::touch::status().gestures.double_taps,1u);
    EXPECT_FALSE(sample(400,true,false));EXPECT_FALSE(sample(450,false,false));
    EXPECT_EQ(dg_board::touch::status().gestures.ignored,1u);
    EXPECT_FALSE(sample(600,true));EXPECT_FALSE(sample(650,false));
    read_result=ESP_ERR_TIMEOUT;EXPECT_FALSE(sample(700,true));read_result=ESP_OK;
    EXPECT_FALSE(sample(750,false));EXPECT_EQ(dg_board::touch::status().read_errors,1u);
    EXPECT_FALSE(sample(800,true));EXPECT_FALSE(sample(850,false));
    irq(nullptr);irq(nullptr);points=1;EXPECT_FALSE(dg_board::touch::poll(900,true));
    EXPECT_TRUE(sample(950,false)); // coalesced edges during a held contact still tap
    EXPECT_EQ(dg_board::touch::status().coalesced,1u);
    EXPECT_EQ(dg_board::touch::status().gestures.double_taps,2u);
    EXPECT_EQ(dg_board::touch::status().reads,reads);
    const auto real=dg_board::touch::status();const auto prior_reads=reads;
    EXPECT_TRUE(dg_board::touch::inject_double_tap());EXPECT_FALSE(dg_board::touch::inject_double_tap());
    EXPECT_TRUE(dg_board::touch::poll(1000,true));EXPECT_FALSE(dg_board::touch::poll(1010,true));
    EXPECT_TRUE(dg_board::touch::inject_double_tap());EXPECT_FALSE(dg_board::touch::poll(1020,false));
    const auto injected=dg_board::touch::status();EXPECT_EQ(injected.injected,2u);EXPECT_EQ(injected.injected_ignored,1u);
    EXPECT_EQ(injected.interrupts,real.interrupts);EXPECT_EQ(injected.gestures.double_taps,real.gestures.double_taps);EXPECT_EQ(reads,prior_reads);
}
