#pragma once
#include "esp_err.h"
#include <cstddef>
#include <cstdint>
using i2c_master_bus_handle_t=void*;
struct TraceI2cDevice {unsigned address;};
using i2c_master_dev_handle_t=TraceI2cDevice*;
constexpr int I2C_ADDR_BIT_LEN_7=7;
struct i2c_device_config_t {int dev_addr_length=0;unsigned device_address=0,scl_speed_hz=0;};
int i2c_master_probe(i2c_master_bus_handle_t,unsigned,int);
int i2c_master_bus_add_device(i2c_master_bus_handle_t,const i2c_device_config_t*,i2c_master_dev_handle_t*);
int i2c_master_transmit(i2c_master_dev_handle_t,const uint8_t*,size_t,int);
int i2c_master_transmit_receive(i2c_master_dev_handle_t,const uint8_t*,size_t,uint8_t*,size_t,int);
