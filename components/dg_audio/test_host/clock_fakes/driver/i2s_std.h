#pragma once
#include <cstdint>
using gpio_num_t=int;using i2s_mclk_multiple_t=int;using i2s_data_bit_width_t=int;using i2s_slot_bit_width_t=int;
#include <stdexcept>
constexpr int ESP_OK=0,ESP_ERR_INVALID_STATE=0x103;
#define ESP_ERROR_CHECK(expr) do {if((expr)!=ESP_OK)throw std::runtime_error("I2S configuration failed");}while(0)
struct i2s_channel_obj_t;
using i2s_chan_handle_t=i2s_channel_obj_t*;
constexpr int I2S_NUM_0=0,I2S_ROLE_MASTER=1,I2S_ROLE_SLAVE=2;
constexpr int I2S_DATA_BIT_WIDTH_16BIT=16,I2S_DATA_BIT_WIDTH_32BIT=32;
constexpr int I2S_SLOT_MODE_STEREO=2,I2S_SLOT_BIT_WIDTH_32BIT=32;
constexpr int I2S_MCLK_MULTIPLE_256=256,I2S_MCLK_MULTIPLE_512=512;
constexpr int I2S_GPIO_UNUSED=-1,GPIO_NUM_42=42,GPIO_NUM_40=40,GPIO_NUM_39=39,GPIO_NUM_41=41,GPIO_NUM_3=3;
struct i2s_chan_config_t {int dma_desc_num=0,dma_frame_num=0;bool auto_clear=false;};
struct i2s_std_clk_config_t {uint32_t sample_rate_hz;int mclk_multiple;};
struct i2s_std_slot_config_t {int data_bit_width,slot_mode,slot_bit_width;uint32_t ws_width;};
struct i2s_std_config_t {
    i2s_std_clk_config_t clk_cfg{};i2s_std_slot_config_t slot_cfg{};
    struct {int mclk,bclk,ws,dout,din;} gpio_cfg{};
};
struct i2s_chan_info_t {int role;uint32_t mclk_hz,bclk_hz;i2s_chan_handle_t pair_chan;};
#define I2S_CHANNEL_DEFAULT_CONFIG(num,role) i2s_chan_config_t{}
#define I2S_STD_CLK_DEFAULT_CONFIG(rate) i2s_std_clk_config_t{rate,I2S_MCLK_MULTIPLE_256}
#define I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(bits,mode) i2s_std_slot_config_t{bits,mode,0,bits}
int i2s_new_channel(const i2s_chan_config_t*,i2s_chan_handle_t*,i2s_chan_handle_t*);
int i2s_channel_init_std_mode(i2s_chan_handle_t,const i2s_std_config_t*);
int i2s_channel_get_info(i2s_chan_handle_t,i2s_chan_info_t*);
int i2s_channel_enable(i2s_chan_handle_t);

struct i2s_event_data_t {};
struct i2s_event_callbacks_t {bool (*on_recv)(i2s_chan_handle_t,i2s_event_data_t*,void*)=nullptr;bool (*on_recv_q_ovf)(i2s_chan_handle_t,i2s_event_data_t*,void*)=nullptr;};
int i2s_channel_register_event_callback(i2s_chan_handle_t,const i2s_event_callbacks_t*,void*);
