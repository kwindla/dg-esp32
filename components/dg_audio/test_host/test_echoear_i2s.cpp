#include "i2s_duplex.h"
#include <gtest/gtest.h>
#include <vector>
struct i2s_channel_obj_t {bool tx;int role=I2S_ROLE_MASTER;i2s_std_config_t config{};};
namespace {
i2s_event_callbacks_t callbacks{};
i2s_channel_obj_t rx{false},tx{true};i2s_chan_config_t allocation;
std::vector<i2s_chan_handle_t> initialized,enabled;int bad_tx_clock=0,bad_tx_bclk=0;
class EchoEarI2s:public testing::Test {
    void SetUp() override {rx={false};tx={true};callbacks={};initialized.clear();enabled.clear();bad_tx_clock=bad_tx_bclk=0;}
};
unsigned frame_bits(const i2s_std_config_t& cfg) {
    return 2*(cfg.slot_cfg.slot_bit_width?cfg.slot_cfg.slot_bit_width:cfg.slot_cfg.data_bit_width);
}
}
int i2s_new_channel(const i2s_chan_config_t* cfg,i2s_chan_handle_t* out,i2s_chan_handle_t* in) {
    allocation=*cfg;*out=&tx;*in=&rx;return ESP_OK;
}
int i2s_channel_init_std_mode(i2s_chan_handle_t handle,const i2s_std_config_t* cfg) {
    // Model only documented initialization-order fallback, not an I2S HAL.
    if(!initialized.empty())handle->role=I2S_ROLE_SLAVE;
    initialized.push_back(handle);handle->config=*cfg;return ESP_OK;
}
int i2s_channel_get_info(i2s_chan_handle_t handle,i2s_chan_info_t* info) {
    *info={handle->role,handle->config.clk_cfg.sample_rate_hz*unsigned(handle->config.clk_cfg.mclk_multiple),
        handle->config.clk_cfg.sample_rate_hz*frame_bits(handle->config),handle->tx?&rx:&tx};
    if(handle->tx && bad_tx_clock)info->mclk_hz=bad_tx_clock;
    if(handle->tx && bad_tx_bclk)info->bclk_hz=bad_tx_bclk;
    return ESP_OK;
}
int i2s_channel_register_event_callback(i2s_chan_handle_t handle,const i2s_event_callbacks_t* cb,void*) {
    EXPECT_EQ(handle,&rx);EXPECT_TRUE(enabled.empty());callbacks=*cb;return ESP_OK;
}
int i2s_channel_enable(i2s_chan_handle_t handle) {enabled.push_back(handle);return ESP_OK;}

TEST_F(EchoEarI2s, AecKeepsExternalCodecClockAndRaisesOnlyInternalTxSlaveRatio) {
    i2s_chan_handle_t in=nullptr,out=nullptr;
    ASSERT_NO_THROW(dg_audio::codec::initialize_i2s(in,out,true));
    EXPECT_EQ(initialized,(std::vector<i2s_chan_handle_t>{&rx,&tx}));
    EXPECT_EQ(enabled,(std::vector<i2s_chan_handle_t>{&rx}));
    EXPECT_EQ(in,&rx);EXPECT_EQ(out,&tx);
    EXPECT_EQ(rx.config.clk_cfg.mclk_multiple,256);EXPECT_EQ(rx.config.gpio_cfg.mclk,42);
    EXPECT_EQ(tx.config.clk_cfg.mclk_multiple,512);EXPECT_EQ(tx.config.gpio_cfg.mclk,I2S_GPIO_UNUSED);
    for(auto* channel:{&rx,&tx}) {
        const auto& cfg=channel->config;
        EXPECT_EQ(cfg.clk_cfg.sample_rate_hz,16000u);EXPECT_EQ(frame_bits(cfg),64u);
        EXPECT_EQ(cfg.slot_cfg.ws_width,32u);
        EXPECT_EQ(cfg.gpio_cfg.bclk,40);EXPECT_EQ(cfg.gpio_cfg.ws,39);
        EXPECT_EQ(cfg.gpio_cfg.dout,41);EXPECT_EQ(cfg.gpio_cfg.din,3);
    }
    EXPECT_EQ(rx.config.slot_cfg.data_bit_width,32);EXPECT_EQ(tx.config.slot_cfg.data_bit_width,16);
    EXPECT_EQ(tx.config.clk_cfg.mclk_multiple/frame_bits(tx.config),8u);
    EXPECT_GE(tx.config.clk_cfg.mclk_multiple/frame_bits(tx.config),6u);
    EXPECT_EQ(allocation.dma_desc_num,4);EXPECT_EQ(allocation.dma_frame_num,240);EXPECT_TRUE(allocation.auto_clear);
}
TEST_F(EchoEarI2s, StandardModeRetainsBoth256fsClocksAnd16bitGeometry) {
    i2s_chan_handle_t in=nullptr,out=nullptr;dg_audio::codec::initialize_i2s(in,out,false);
    for(auto* channel:{&rx,&tx}) {
        EXPECT_EQ(channel->config.clk_cfg.mclk_multiple,256);EXPECT_EQ(frame_bits(channel->config),32u);
        EXPECT_EQ(channel->config.slot_cfg.data_bit_width,16);EXPECT_EQ(channel->config.gpio_cfg.mclk,42);
    }
    EXPECT_EQ(initialized.front(),&rx);EXPECT_EQ(enabled,(std::vector<i2s_chan_handle_t>{&rx}));
}
TEST_F(EchoEarI2s, RuntimeRejectsTheOldFourToOneInternalTxClock) {
    bad_tx_clock=4096000;i2s_chan_handle_t in=nullptr,out=nullptr;
    EXPECT_THROW(dg_audio::codec::initialize_i2s(in,out,true),std::runtime_error);
    EXPECT_TRUE(enabled.empty());
}
TEST_F(EchoEarI2s, RuntimeRejectsUnexpectedBclkBeforeStartingCapture) {
    bad_tx_bclk=1536000;i2s_chan_handle_t in=nullptr,out=nullptr;
    EXPECT_THROW(dg_audio::codec::initialize_i2s(in,out,true),std::runtime_error);
    EXPECT_TRUE(enabled.empty());
}

TEST_F(EchoEarI2s, ReceiveCallbacksCountCompletionsAndOverflowsIndependently) {
    i2s_chan_handle_t in=nullptr,out=nullptr;dg_audio::codec::initialize_i2s(in,out,true);
    ASSERT_NE(callbacks.on_recv,nullptr);ASSERT_NE(callbacks.on_recv_q_ovf,nullptr);
    const auto completed=dg_audio::codec::capture_completions(),overflow=dg_audio::codec::capture_overflows();
    for(int i=0;i<7;++i)EXPECT_FALSE(callbacks.on_recv(in,nullptr,nullptr));
    EXPECT_FALSE(callbacks.on_recv_q_ovf(in,nullptr,nullptr));
    EXPECT_EQ(dg_audio::codec::capture_completions()-completed,7u);
    EXPECT_EQ(dg_audio::codec::capture_overflows()-overflow,1u);
}
