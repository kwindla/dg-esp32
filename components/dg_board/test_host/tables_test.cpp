#include <gtest/gtest.h>
#include "sdkconfig.h"
#include "dg_board/board.h"
#include "dg_board/audio_hw.h"
#include "dg_board/power.h"
using namespace dg_board;
// Board and audio constants are pinned independently of their consumers.
// Board sources: components/dg_board/src/{echoear,waveshare146}/.
// Audio backends: components/dg_audio/src/codec/; public headers: include/dg_audio/.
// panel_config.h: components/dg_display/include/dg_display/. IDF citations are v6.1.
TEST(BoardInfo,NonNullHookAndErrorContract){
    const auto& b=info();ASSERT_NE(b.name,nullptr);ASSERT_NE(b.chip,nullptr);
    ASSERT_NE(b.simple_ui_error,nullptr);EXPECT_NE(*b.simple_ui_error,0);ASSERT_NE(b.log_audio_boot,nullptr);
    for(bool aec:{false,true}) {
        const auto& a=audio_hw(aec);
        ASSERT_NE(a.analog_gain_error,nullptr);EXPECT_NE(*a.analog_gain_error,0);
        ASSERT_NE(a.reference_gain_error,nullptr);EXPECT_NE(*a.reference_gain_error,0);
        ASSERT_NE(a.codec_log_tag,nullptr);EXPECT_NE(*a.codec_log_tag,0);
        ASSERT_NE(a.tdm_clock_description,nullptr);EXPECT_NE(*a.tdm_clock_description,0);
        EXPECT_NE(a.log_paired_clocks,nullptr);EXPECT_NE(a.log_codec_summary,nullptr);
        EXPECT_NE(a.log_codec_probe,nullptr);EXPECT_NE(a.log_aec_guard,nullptr);EXPECT_NE(a.log_aec_init,nullptr);
    }
}
#if CONFIG_DG_BOARD_P4XC
TEST(BoardInfo,HeadlessP4Capabilities){
    const auto& b=info();EXPECT_STREQ(b.name,"p4xc");EXPECT_STREQ(b.chip,"esp32p4");
    EXPECT_EQ(b.panel.width,800u);EXPECT_EQ(b.panel.height,800u);EXPECT_TRUE(b.panel.round);
    EXPECT_EQ(b.touch,TouchKind::Polled);EXPECT_EQ(b.console,ConsoleKind::Uart);
    EXPECT_FALSE(b.has_battery);EXPECT_FALSE(power::available());EXPECT_TRUE(b.aec_reference);
    EXPECT_FALSE(b.internal_8bit_dma_alias);EXPECT_FALSE(b.cache_counters);EXPECT_TRUE(b.simple_ui);
    const auto& a=audio_hw();EXPECT_EQ(&a,&audio_hw(true));EXPECT_EQ(a.codec,CodecKind::Es8311Es7210);
    EXPECT_EQ(a.board_rail.gpio,-1);EXPECT_EQ(a.codec_rail.gpio,-1);EXPECT_EQ(a.pa.gpio,53);EXPECT_TRUE(a.pa.active_high);
    EXPECT_EQ(a.dac_i2c_address,0x18);EXPECT_EQ(a.adc_i2c_address,0x40);
    EXPECT_EQ(a.rx.port,0);EXPECT_EQ(a.tx.port,0);
    EXPECT_EQ(a.rx.bclk_gpio,12);EXPECT_EQ(a.tx.bclk_gpio,12);EXPECT_EQ(a.rx.ws_gpio,10);EXPECT_EQ(a.tx.ws_gpio,10);
    EXPECT_EQ(a.rx.data_gpio,11);EXPECT_EQ(a.tx.data_gpio,9);
    EXPECT_EQ(a.rx.data_bits,32u);EXPECT_EQ(a.rx.slot_bits,32u);EXPECT_EQ(a.rx.ws_width,32u);
    EXPECT_EQ(a.tx.data_bits,16u);EXPECT_EQ(a.tx.slot_bits,32u);EXPECT_EQ(a.tx.ws_width,32u);
    EXPECT_EQ(a.rx.clock.sample_rate_hz,16000u);EXPECT_EQ(a.tx.clock.sample_rate_hz,16000u);
    EXPECT_EQ(a.rx.clock.mclk_multiple,256u);EXPECT_EQ(a.tx.clock.mclk_multiple,512u);
    EXPECT_EQ(a.rx.clock.mclk_gpio,13);EXPECT_EQ(a.tx.clock.mclk_gpio,-1);
    EXPECT_EQ(a.rx.clock.role,I2sRole::Master);EXPECT_EQ(a.tx.clock.role,I2sRole::Slave);
    EXPECT_TRUE(a.rx.clock.owns_external_mclk);EXPECT_FALSE(a.tx.clock.owns_external_mclk);
    EXPECT_EQ(a.rx.selected_slot,I2sSlot::Left);EXPECT_EQ(a.tx.selected_slot,I2sSlot::Stereo);
    EXPECT_EQ(a.rx.dma_descriptors,unsigned(CONFIG_DG_SPEAKER_DMA_DESCRIPTORS));
    EXPECT_EQ(a.rx.dma_frames,unsigned(CONFIG_DG_SPEAKER_DMA_FRAMES));
    EXPECT_EQ(a.tx.dma_descriptors,a.rx.dma_descriptors);EXPECT_EQ(a.tx.dma_frames,a.rx.dma_frames);
    EXPECT_TRUE(a.aec_reference);EXPECT_TRUE(a.analog_gain_control);EXPECT_TRUE(a.microphone_limiter);EXPECT_TRUE(a.adc_clip_counting);
    EXPECT_EQ(a.default_output_gain_db,-6);EXPECT_STREQ(a.codec_log_tag,"p4xc-audio");
    const auto& r=a.registers;EXPECT_FALSE(r.no_dac_reference);EXPECT_TRUE(r.use_mclk);EXPECT_EQ(r.codec_pa_gpio,-1);
    EXPECT_EQ(r.hardware_pa_gain_db,0);EXPECT_EQ(r.mic_mask,7u);EXPECT_EQ(r.analog_gain_db,30u);
    EXPECT_EQ(r.adc_format_bits,32u);EXPECT_EQ(r.mclk_multiple,256u);EXPECT_EQ(r.dac_volume_db,-6);
    EXPECT_EQ(r.reg0d.reg,0x0d);EXPECT_EQ(r.reg0d.value,0x01);EXPECT_EQ(r.reg44.reg,0x44);EXPECT_EQ(r.reg44.value,0x58);
    EXPECT_EQ(r.mic1_gain.reg,0x43);EXPECT_EQ(r.mic1_gain.value,0x1a);
    EXPECT_EQ(r.mclk_multiple,a.rx.clock.mclk_multiple);
    const auto& limits=a.paired_clock_limits;
    EXPECT_EQ(limits.rx_mclk_min,4095000u);EXPECT_EQ(limits.rx_mclk_max,4097000u);
    EXPECT_EQ(limits.tx_mclk_min,8190000u);EXPECT_EQ(limits.tx_mclk_max,8194000u);
    EXPECT_EQ(limits.bclk_min,1023500u);EXPECT_EQ(limits.bclk_max,1024500u);
    EXPECT_EQ(limits.tx_mclk_bclk_min_ratio,6u);
}
#else
TEST(BoardInfo,NativeGeometryAndCapabilities){
    const auto& b=info();EXPECT_EQ(&b,&info());EXPECT_STREQ(b.chip,"esp32s3");
    EXPECT_TRUE(b.internal_8bit_dma_alias);EXPECT_TRUE(b.cache_counters);EXPECT_NE(b.log_audio_boot,nullptr);
    EXPECT_EQ(b.simple_ui,b.panel.width==360);EXPECT_STREQ(b.simple_ui_error,"demo needs EchoEar panel");
    EXPECT_TRUE(b.panel.round);EXPECT_EQ(b.console,ConsoleKind::UsbSerialJtag);
#if CONFIG_DG_BOARD_ECHOEAR
    // panel_config.h:13-16; touch.cpp:28-48; power.cpp:13-50.
    EXPECT_STREQ(b.name,"echoear");EXPECT_EQ(b.panel.width,360u);EXPECT_EQ(b.panel.height,360u);
    EXPECT_EQ(b.touch,TouchKind::Irq);EXPECT_TRUE(b.has_battery);EXPECT_TRUE(b.aec_reference);EXPECT_TRUE(power::available());
#else
    // panel_config.h:17-20; touch.cpp:45-47,70-72; power.cpp:43-53.
    EXPECT_STREQ(b.name,"waveshare_146");EXPECT_EQ(b.panel.width,412u);EXPECT_EQ(b.panel.height,412u);
    EXPECT_EQ(b.touch,TouchKind::None);EXPECT_FALSE(b.has_battery);EXPECT_FALSE(b.aec_reference);EXPECT_FALSE(power::available());
#endif
}
TEST(BoardInfo,SchemaCanDescribeP4WithoutImplementingIt){
    constexpr BoardInfo p4{"p4xc","esp32p4",{800,800,true},false,TouchKind::Polled,true,ConsoleKind::Uart};
    EXPECT_EQ(p4.panel.width,800u);EXPECT_EQ(p4.panel.height,800u);EXPECT_TRUE(p4.panel.round);
    EXPECT_EQ(p4.touch,TouchKind::Polled);EXPECT_FALSE(p4.has_battery);EXPECT_EQ(p4.console,ConsoleKind::Uart);
    constexpr AudioHw null{};EXPECT_EQ(null.codec,CodecKind::Null);
}
TEST(AudioHw,PortsClocksDmaAndPrimarySelection){
    const auto& a=audio_hw();EXPECT_EQ(&a,&audio_hw());
    // codec/i2s_duplex.cpp and codec/direct_i2s.cpp: channel/stream configuration.
    EXPECT_EQ(a.rx.clock.sample_rate_hz,16000u);EXPECT_EQ(a.tx.clock.sample_rate_hz,16000u);
    EXPECT_EQ(a.rx.clock.mclk_multiple,256u);EXPECT_EQ(a.tx.clock.mclk_multiple,256u);
    EXPECT_EQ(a.rx.clock.role,I2sRole::Master);
    EXPECT_EQ(a.rx.slot,I2sSlot::Stereo);EXPECT_EQ(a.tx.slot,I2sSlot::Stereo);
    EXPECT_EQ(a.tx.data_bits,16u);EXPECT_EQ(a.tx.slot_bits,16u);EXPECT_EQ(a.tx.ws_width,16u);
    EXPECT_EQ(a.tx.dma_descriptors,unsigned(CONFIG_DG_SPEAKER_DMA_DESCRIPTORS));
    EXPECT_EQ(a.tx.dma_frames,unsigned(CONFIG_DG_SPEAKER_DMA_FRAMES));EXPECT_TRUE(a.tx.auto_clear);
#if CONFIG_DG_BOARD_ECHOEAR
    // codec/i2s_duplex.cpp: paired port0, speaker DMA for BOTH directions.
    EXPECT_EQ(a.codec,CodecKind::Es8311Es7210);EXPECT_EQ(a.rx.port,0);EXPECT_EQ(a.tx.port,0);
    EXPECT_EQ(a.rx.data_bits,16u);EXPECT_EQ(a.rx.slot_bits,16u);EXPECT_EQ(a.rx.ws_width,16u);
    EXPECT_EQ(a.rx.dma_descriptors,unsigned(CONFIG_DG_SPEAKER_DMA_DESCRIPTORS));
    EXPECT_EQ(a.rx.dma_frames,unsigned(CONFIG_DG_SPEAKER_DMA_FRAMES));EXPECT_TRUE(a.rx.auto_clear);
    EXPECT_EQ(a.rx.bclk_gpio,40);EXPECT_EQ(a.tx.bclk_gpio,40);EXPECT_EQ(a.rx.ws_gpio,39);EXPECT_EQ(a.tx.ws_gpio,39);
    EXPECT_EQ(a.rx.data_gpio,3);EXPECT_EQ(a.tx.data_gpio,41);
    EXPECT_EQ(a.rx.clock.mclk_gpio,42);EXPECT_EQ(a.tx.clock.mclk_gpio,42);
    EXPECT_TRUE(a.rx.clock.owns_external_mclk);EXPECT_FALSE(a.tx.clock.owns_external_mclk);
    // IDF6.1 i2s_common.c:198-202 / i2s_std.c:209-229: RX initialized first.
    EXPECT_EQ(a.tx.clock.role,I2sRole::Slave);
    // codec/es7210_slots.h + mic.cpp: normal stereo16 low/high signed halves.
    EXPECT_EQ(a.rx.selected_slot,I2sSlot::Left);EXPECT_EQ(a.microphones[0].word,0);EXPECT_EQ(a.microphones[0].shift,0u);
    EXPECT_EQ(a.microphones[1].word,0);EXPECT_EQ(a.microphones[1].shift,16u);EXPECT_EQ(a.microphones[2].word,-1);
#else
    // codec/direct_i2s.cpp + mic.cpp: port0 stereo32, right word high16.
    EXPECT_EQ(a.codec,CodecKind::DirectI2s);EXPECT_EQ(a.rx.port,0);EXPECT_EQ(a.tx.port,1);
    EXPECT_EQ(a.rx.data_bits,32u);EXPECT_EQ(a.rx.slot_bits,32u);EXPECT_EQ(a.rx.ws_width,32u);
    EXPECT_EQ(a.rx.selected_slot,I2sSlot::Right);EXPECT_EQ(a.microphones[0].word,1);EXPECT_EQ(a.microphones[0].shift,16u);
    EXPECT_EQ(a.rx.bclk_gpio,15);EXPECT_EQ(a.rx.ws_gpio,2);EXPECT_EQ(a.rx.data_gpio,39);
    EXPECT_EQ(a.rx.dma_descriptors,unsigned(CONFIG_DG_MIC_DMA_DESCRIPTORS));EXPECT_EQ(a.rx.dma_frames,unsigned(CONFIG_DG_MIC_DMA_FRAMES));
    EXPECT_FALSE(a.rx.auto_clear);EXPECT_EQ(a.rx.clock.mclk_gpio,-1);EXPECT_EQ(a.tx.clock.mclk_gpio,-1);
    EXPECT_FALSE(a.rx.clock.owns_external_mclk);EXPECT_FALSE(a.tx.clock.owns_external_mclk);
    // codec/direct_i2s.cpp: independent master port1, stereo16, no MCLK pin.
    EXPECT_EQ(a.tx.clock.role,I2sRole::Master);EXPECT_EQ(a.tx.bclk_gpio,48);EXPECT_EQ(a.tx.ws_gpio,38);EXPECT_EQ(a.tx.data_gpio,47);
#endif
}
TEST(AudioHw,GainProtectionAndCodecRegisters){
    const auto& a=audio_hw();
#if CONFIG_DG_BOARD_ECHOEAR
    // codec/es8311_es7210.cpp + codec/playback.cpp; echoear/board.cpp rail.
    EXPECT_EQ(a.dac_i2c_address,0x18);EXPECT_EQ(a.adc_i2c_address,0x40);
    EXPECT_EQ(a.board_rail.gpio,9);EXPECT_FALSE(a.board_rail.active_high);
    EXPECT_EQ(a.codec_rail.gpio,48);EXPECT_TRUE(a.codec_rail.active_high);EXPECT_EQ(a.pa.gpio,15);EXPECT_TRUE(a.pa.active_high);
    // codec/es8311_es7210.cpp: exact codec setup/readback profile.
    const auto& r=a.registers;EXPECT_FALSE(r.no_dac_reference);EXPECT_TRUE(r.use_mclk);EXPECT_EQ(r.codec_pa_gpio,-1);
    EXPECT_EQ(r.hardware_pa_gain_db,6);EXPECT_EQ(r.mic_mask,3u);EXPECT_EQ(r.analog_gain_db,30u);
    EXPECT_EQ(r.adc_format_bits,16u);EXPECT_EQ(r.mclk_multiple,256u);EXPECT_EQ(r.dac_volume_db,-6);
    EXPECT_EQ(r.reg0d.reg,0x0d);EXPECT_EQ(r.reg0d.value,0x01);EXPECT_EQ(r.reg44.reg,0x44);EXPECT_EQ(r.reg44.value,0x58);
    EXPECT_EQ(r.mic1_gain.reg,0x43);EXPECT_EQ(r.mic1_gain.value,0x1a);
    // mic.cpp protection flags; pipeline.cpp default/analog gain; codec gain control.
    EXPECT_TRUE(a.microphone_limiter);EXPECT_TRUE(a.adc_clip_counting);EXPECT_TRUE(a.analog_gain_control);EXPECT_EQ(a.default_output_gain_db,-6);
#else
    // mic.cpp protection flags; pipeline.cpp default/analog gain; speaker.h Q15 default.
    EXPECT_FALSE(a.microphone_limiter);EXPECT_FALSE(a.adc_clip_counting);EXPECT_FALSE(a.analog_gain_control);
    EXPECT_EQ(a.default_output_gain_db,0);EXPECT_EQ(a.dac_i2c_address,-1);EXPECT_EQ(a.adc_i2c_address,-1);
    EXPECT_EQ(a.pa.gpio,-1);EXPECT_EQ(a.codec_rail.gpio,-1);EXPECT_EQ(a.board_rail.gpio,-1);
#endif
}
TEST(AudioHw,AecClockOwnershipAndWireSlotMap){
    const auto& a=audio_hw(true);
#if CONFIG_DG_BOARD_ECHOEAR
    // codec/{i2s_duplex.cpp,es7210_slots.h,es8311_es7210.cpp}: AEC profile.
    EXPECT_TRUE(a.aec_reference);EXPECT_EQ(a.rx.data_bits,32u);EXPECT_EQ(a.rx.slot_bits,32u);EXPECT_EQ(a.rx.ws_width,32u);
    EXPECT_EQ(a.tx.data_bits,16u);EXPECT_EQ(a.tx.slot_bits,32u);EXPECT_EQ(a.tx.ws_width,32u);
    EXPECT_EQ(a.rx.clock.mclk_multiple,256u);EXPECT_EQ(a.tx.clock.mclk_multiple,512u);
    EXPECT_EQ(a.rx.clock.mclk_gpio,42);EXPECT_EQ(a.tx.clock.mclk_gpio,-1);
    EXPECT_TRUE(a.rx.clock.owns_external_mclk);EXPECT_FALSE(a.tx.clock.owns_external_mclk);
    EXPECT_EQ(a.rx.clock.role,I2sRole::Master);EXPECT_EQ(a.tx.clock.role,I2sRole::Slave);
    EXPECT_EQ(a.microphones[0].word,0);EXPECT_EQ(a.microphones[0].shift,16u);
    EXPECT_EQ(a.microphones[1].word,1);EXPECT_EQ(a.microphones[1].shift,16u);
    EXPECT_EQ(a.microphones[2].word,0);EXPECT_EQ(a.microphones[2].shift,0u);
    EXPECT_EQ(a.microphones[3].word,1);EXPECT_EQ(a.microphones[3].shift,0u);
    EXPECT_EQ(a.registers.mic_mask,7u);EXPECT_EQ(a.registers.adc_format_bits,32u);
#else
    EXPECT_EQ(&a,&audio_hw(false));EXPECT_FALSE(a.aec_reference);
#endif
}

#endif
