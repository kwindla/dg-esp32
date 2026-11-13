#include "dg_board/audio_hw.h"
#include "sdkconfig.h"
#include "esp_log.h"
namespace dg_board {
namespace {
// Compatibility records are board facts; keep the frozen S3 log bytes intact.
void paired_clocks([[maybe_unused]] unsigned long rx_mclk,[[maybe_unused]] unsigned long rx_bclk,[[maybe_unused]] unsigned long tx_mclk,[[maybe_unused]] unsigned long tx_bclk){
    ESP_LOGI("echoear-audio","AEC I2S RX_MASTER external_MCLK=%lu BCLK=%lu TX_SLAVE internal_MCLK=%lu BCLK=%lu requested_TX_div=8 min=6 GPIO42=RX",rx_mclk,rx_bclk,tx_mclk,tx_bclk);
}
void codec_summary(){
#if CONFIG_DG_ECHOEAR_AEC
    ESP_LOGI("echoear-audio","ES7210 TDM MIC1=30dB MIC3=selected reference; ES8311 requested=-6dB register=-8.5dB PA=15; AEC UNQUALIFIED");
#else
    ESP_LOGI("echoear-audio","ES7210 MIC1 mono from stereo, gain=30dB; ES8311 requested=-6dB register=-8.5dB PA=15; 16000Hz/16bit; AEC off");
#endif
}
void codec_probe([[maybe_unused]] bool ready,[[maybe_unused]] bool dac,[[maybe_unused]] bool adc){
    if(ready){ESP_LOGI("echoear-audio","codec probes 0x18/0x40 ready before 200ms rail deadline");}
    else {ESP_LOGE("echoear-audio","codec probe deadline: 0x18=%d 0x40=%d",dac,adc);}
}
void aec_guard([[maybe_unused]] unsigned guard,[[maybe_unused]] unsigned rearm){
    ESP_LOGI("dg-audio","AEC_GUARD_CONFIG guard_ms=%u afe_delay_samples=1024 rearm_ms=%u",guard,rearm);
}
void aec_init([[maybe_unused]] unsigned chunk,[[maybe_unused]] unsigned delay,[[maybe_unused]] int internal,[[maybe_unused]] int psram){
    ESP_LOGI("echoear-aec","UNQUALIFIED esp-sr=2.4.7 AFE_AEC FD_LOW_COST filter=4 MR=MIC1,MIC3 chunk=%u adapter_delay=%u internal_delta=%d psram_delta=%d NS=0 AGC=0",chunk,delay,internal,psram);
}
constexpr AudioHw profile(bool aec){
    AudioHw value;
    value.codec=CodecKind::Es8311Es7210;
    value.analog_gain_error="Analog gain is EchoEar only";
    value.reference_gain_error="Reference gain requires EchoEar AEC";
    value.codec_log_tag="echoear-audio";
    value.tdm_clock_description="RX=stereo32 TX=data16/slot32 BCLK=1024000 WS=16000";
    value.log_aec_guard=aec_guard;
    value.log_paired_clocks=paired_clocks;value.log_codec_summary=codec_summary;value.log_codec_probe=codec_probe;
    value.log_aec_init=aec_init;
    value.dac_i2c_address=0x18;value.adc_i2c_address=0x40;
    value.board_rail={9,false};value.codec_rail={48,true};value.pa={15,true};
    // RX records the shared cc.auto_clear request; IDF uses auto_clear only
    // for TX. A future per-direction consumer must ignore rx.auto_clear.
    value.rx={0,40,39,3,16,16,16,I2sSlot::Stereo,{16000,256,I2sRole::Master,42,true},
              CONFIG_DG_SPEAKER_DMA_DESCRIPTORS,CONFIG_DG_SPEAKER_DMA_FRAMES,true};
    // IDF makes the second-initialized paired channel a clock slave. Normal
    // mode still requests GPIO42; AEC explicitly leaves TX's MCLK unbound.
    value.tx={0,40,39,41,16,16,16,I2sSlot::Stereo,{16000,256,I2sRole::Slave,42,false},
              CONFIG_DG_SPEAKER_DMA_DESCRIPTORS,CONFIG_DG_SPEAKER_DMA_FRAMES,true};
    value.microphones={AudioSlot{0,0},AudioSlot{0,16},AudioSlot{},AudioSlot{}};
    value.rx.selected_slot=I2sSlot::Left;
    value.registers={false,true,-1,6,3,30,16,256,-6,{0x0d,0x01},{0x44,0x58},{0x43,0x1a}};
    value.analog_gain_control=true;value.microphone_limiter=true;value.adc_clip_counting=true;
    value.default_output_gain_db=-6;
    if(aec){
        value.rx.data_bits=32;value.rx.slot_bits=32;value.rx.ws_width=32;
        value.tx.slot_bits=32;value.tx.ws_width=32;
        value.tx.clock.mclk_multiple=512;value.tx.clock.mclk_gpio=-1;
        value.microphones={AudioSlot{0,16},AudioSlot{1,16},AudioSlot{0,0},AudioSlot{1,0}};
        value.registers.mic_mask=7;value.registers.adc_format_bits=32;
        value.aec_reference=true;
        value.paired_clock_limits={4095000,4097000,8190000,8194000,1023500,1024500,6};
    }
    return value;
}
constexpr auto stereo=profile(false),reference=profile(true);
}
const AudioHw& audio_hw(bool aec){return aec?reference:stereo;}
}
