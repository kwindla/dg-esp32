// GT911 register protocol, as measured by spikes/p4_probe/main/audio_b.cpp.
// GT9271 reports ASCII "9271". No INT pin, reset pulse, task or IRQ emulation.
#include "dg_board/touch.h"
#include "dg_board/i2c.h"
#include "../touch_backend.h"
#include "reset.h"
#include "esp_log.h"
#include "esp_timer.h"
#include <cstring>
#include <initializer_list>
namespace dg_board {
namespace {
i2c_master_dev_handle_t device=nullptr;
uint32_t generation=0;
int64_t next_probe_us=0;
struct BusGuard {
    SemaphoreHandle_t mutex=i2c::mutex();
    bool held=mutex && xSemaphoreTake(mutex,pdMS_TO_TICKS(10)+1)==pdTRUE;
    ~BusGuard(){if(held)xSemaphoreGive(mutex);}
};
esp_err_t read(unsigned reg,uint8_t* bytes,size_t count){
    const uint8_t address[]={uint8_t(reg>>8),uint8_t(reg)};
    return i2c_master_transmit_receive(device,address,2,bytes,count,10);
}
esp_err_t probe(){
    if(device){auto old=device;device=nullptr;auto error=i2c_master_bus_rm_device(old);if(error!=ESP_OK)return error;}
    esp_err_t error=ESP_ERR_INVALID_RESPONSE;
    for(unsigned address:{0x5du,0x14u}){
        i2c_device_config_t config{};config.dev_addr_length=I2C_ADDR_BIT_LEN_7;
        config.device_address=address;config.scl_speed_hz=400000;
        error=i2c_master_bus_add_device(i2c::bus(),&config,&device);
        if(error!=ESP_OK){device=nullptr;continue;}
        uint8_t product[4]{},size[4]{};
        error=read(0x8140,product,sizeof product);
        if(error==ESP_OK && std::memcmp(product,"9271",4) && std::memcmp(product,"911\0",4))error=ESP_ERR_INVALID_RESPONSE;
        if(error==ESP_OK)error=read(0x8048,size,sizeof size);
        if(error==ESP_OK && ((size[0]|size[1]<<8)!=800 || (size[2]|size[3]<<8)!=800))error=ESP_ERR_INVALID_RESPONSE;
        if(error==ESP_OK){
            generation=detail::lcd_reset_generation();
            ESP_LOGI("gt911","product=%.4s address=0x%02x native=800x800 polled<=50Hz",product,address);
            return ESP_OK;
        }
        auto old=device;device=nullptr;auto removed=i2c_master_bus_rm_device(old);if(removed!=ESP_OK)return removed;
    }
    return error;
}
esp_err_t reprobe(){
    const auto now=esp_timer_get_time();
    if(now<next_probe_us)return ESP_ERR_NOT_FINISHED;
    auto error=probe();
    next_probe_us=error==ESP_OK?0:now+1000000;
    return error;
}
}
namespace detail {
uint32_t touch_interrupt_count(){return 0;}
esp_err_t touch_hardware_init(){
    if(!i2c::bus())return ESP_ERR_INVALID_STATE;
    BusGuard lock;if(!lock.held)return ESP_ERR_TIMEOUT;
    return reprobe();
}
}
esp_err_t touch::read_raw(TouchPoint& point){
    if(!i2c::bus())return ESP_ERR_INVALID_STATE;
    BusGuard lock;if(!lock.held)return ESP_ERR_TIMEOUT;
    if(!device || generation!=detail::lcd_reset_generation()){
        auto error=reprobe();
        // Force the gesture owner to cancel pre-reset contacts even when the
        // re-probe succeeds. The next poll may consume the first new sample.
        return error==ESP_OK?ESP_ERR_INVALID_STATE:error;
    }
    uint8_t status=0,data[8]{};
    auto error=read(0x814e,&status,1);
    if(error!=ESP_OK)return error;
    if(!(status&0x80))return ESP_ERR_NOT_FINISHED;
    const auto count=uint8_t(status&15);
    // Only the first point is needed: the gesture owner rejects multi-touch.
    if(count==1)error=read(0x814f,data,sizeof data);
    const uint8_t clear[]={0x81,0x4e,0};
    const auto cleared=i2c_master_transmit(device,clear,sizeof clear,10);
    if(error!=ESP_OK)return error;
    if(cleared!=ESP_OK)return cleared;
    if(count>10)return ESP_ERR_INVALID_RESPONSE;
    point={uint16_t(data[1]|data[2]<<8),uint16_t(data[3]|data[4]<<8),count};
    return ESP_OK;
}
}
