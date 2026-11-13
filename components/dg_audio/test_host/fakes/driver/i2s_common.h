#pragma once
#include <cstddef>
#include <stdexcept>
struct i2s_channel_obj_t;
using i2s_chan_handle_t=i2s_channel_obj_t*;
struct i2s_event_data_t {size_t size=0;};
using i2s_callback=bool(*)(i2s_chan_handle_t,i2s_event_data_t*,void*);
struct i2s_event_callbacks_t {i2s_callback on_sent=nullptr,on_send_q_ovf=nullptr,on_recv=nullptr,on_recv_q_ovf=nullptr;};
#define ESP_OK 0
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_TIMEOUT 0x107
#define ESP_ERROR_CHECK(expr) do {if((expr)!=0)throw std::runtime_error("fake I2S error");}while(0)
int i2s_channel_register_event_callback(i2s_chan_handle_t,const i2s_event_callbacks_t*,void*);
int i2s_channel_enable(i2s_chan_handle_t);
int i2s_channel_disable(i2s_chan_handle_t);
int i2s_del_channel(i2s_chan_handle_t);
int i2s_channel_preload_data(i2s_chan_handle_t,const void*,size_t,size_t*);
int i2s_channel_write(i2s_chan_handle_t,const void*,size_t,size_t*,unsigned);
