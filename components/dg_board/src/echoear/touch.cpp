#include "dg_board/touch.h"
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "../touch_backend.h"
#include "driver/gpio.h"
#include <atomic>
namespace dg_board {
namespace {
i2c_master_dev_handle_t device=nullptr;
std::atomic<uint32_t> irq_count{0};
// Sole ISR writer; no I2C, allocation, logging or atomic RMW.
void interrupt(void*) {irq_count.store(irq_count.load(std::memory_order_relaxed)+1,std::memory_order_relaxed);}
}
namespace detail {
uint32_t touch_interrupt_count(){return irq_count.load(std::memory_order_relaxed);}
esp_err_t touch_hardware_init(){
    if(!i2c::bus())return ESP_ERR_INVALID_STATE;
    i2c_device_config_t cfg{};cfg.dev_addr_length=I2C_ADDR_BIT_LEN_7;
    cfg.device_address=0x15;cfg.scl_speed_hz=400000;
    auto result=i2c_master_bus_add_device(i2c::bus(),&cfg,&device);
    if(result!=ESP_OK)return result;
    gpio_config_t pin{};pin.pin_bit_mask=1ull<<10;pin.mode=GPIO_MODE_INPUT;
    pin.intr_type=GPIO_INTR_NEGEDGE;
    result=gpio_config(&pin);
    if(result==ESP_OK)result=init_gpio_isr_service();
    if(result==ESP_OK)result=gpio_isr_handler_add(GPIO_NUM_10,interrupt,nullptr);
    if(result!=ESP_OK){i2c_master_bus_rm_device(device);device=nullptr;return result;}
    // CST816 may sleep until a touch; preserve the no-probe/no-reset init.
    return ESP_OK;
}
}
esp_err_t touch::read_raw(TouchPoint& point){
    if(!device)return ESP_ERR_INVALID_STATE;
    uint8_t reg=0x02,data[5]{};
    auto result=i2c_master_transmit_receive(device,&reg,1,data,sizeof(data),10);
    if(result==ESP_OK)point={uint16_t(((data[1]&15)<<8)|data[2]),uint16_t(((data[3]&15)<<8)|data[4]),data[0]};
    return result;
}
}
