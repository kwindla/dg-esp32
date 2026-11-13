#pragma once
#include "dg_board/audio_hw.h"
#include "driver/i2s_std.h"
namespace dg_audio::codec {
inline i2s_std_config_t stream_config(const dg_board::I2sStream& stream,int dout,int din,bool explicit_slots=false) {
    i2s_std_config_t sc{};
    sc.clk_cfg=I2S_STD_CLK_DEFAULT_CONFIG(stream.clock.sample_rate_hz);
    sc.clk_cfg.mclk_multiple=i2s_mclk_multiple_t(stream.clock.mclk_multiple);
    sc.slot_cfg=I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(i2s_data_bit_width_t(stream.data_bits),I2S_SLOT_MODE_STEREO);
    // Keep the original AUTO slot width in stereo16 and direct stereo32.
    // ES7210 duplex stereo32 explicitly requested a 32-bit wire slot.
    if(stream.slot_bits!=stream.data_bits || explicit_slots)
        sc.slot_cfg.slot_bit_width=i2s_slot_bit_width_t(stream.slot_bits);
    sc.slot_cfg.ws_width=stream.ws_width;
    sc.gpio_cfg.mclk=gpio_num_t(stream.clock.mclk_gpio);
    sc.gpio_cfg.bclk=gpio_num_t(stream.bclk_gpio);sc.gpio_cfg.ws=gpio_num_t(stream.ws_gpio);
    sc.gpio_cfg.dout=gpio_num_t(dout);sc.gpio_cfg.din=gpio_num_t(din);
    return sc;
}
inline i2s_chan_config_t channel_config(const dg_board::I2sStream& stream) {
    const auto role=stream.clock.role==dg_board::I2sRole::Master?I2S_ROLE_MASTER:I2S_ROLE_SLAVE;
    i2s_chan_config_t cc=I2S_CHANNEL_DEFAULT_CONFIG(stream.port,role);
    cc.dma_desc_num=stream.dma_descriptors;cc.dma_frame_num=stream.dma_frames;
    cc.auto_clear=stream.auto_clear;
    return cc;
}
}
