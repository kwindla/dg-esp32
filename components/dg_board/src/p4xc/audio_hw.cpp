// Wiring/clock/slot source: spikes/p4_probe/main/main.cpp; docs/p4xc.md Facts A/B.
#include "dg_board/audio_hw.h"
#include "sdkconfig.h"
#include "esp_log.h"
namespace dg_board {
namespace {
void paired_clocks([[maybe_unused]] unsigned long rx_mclk,[[maybe_unused]] unsigned long rx_bclk,[[maybe_unused]] unsigned long tx_mclk,[[maybe_unused]] unsigned long tx_bclk){
    ESP_LOGI("p4xc-audio","I2S RX_MASTER external_MCLK=%lu BCLK=%lu TX_SLAVE internal_MCLK=%lu BCLK=%lu requested_TX_div=8 min=6 GPIO13=RX",rx_mclk,rx_bclk,tx_mclk,tx_bclk);
}
void codec_summary(){
    ESP_LOGI("p4xc-audio","ES7210 TDM MIC1=30dB MIC2=w1/high MIC3=w0/low electrical reference MIC4=absent; ES8311 requested=-6dB register=-2.5dB PA=53; levels UNMEASURED");
}
void codec_probe([[maybe_unused]] bool ready,[[maybe_unused]] bool dac,[[maybe_unused]] bool adc){
    if(ready){ESP_LOGI("p4xc-audio","codec probes 0x18/0x40 ready before 200ms deadline (no rail)");}
    else {ESP_LOGE("p4xc-audio","codec probe deadline: 0x18=%d 0x40=%d",dac,adc);}
}
void aec_guard([[maybe_unused]] unsigned guard,[[maybe_unused]] unsigned rearm){
    ESP_LOGI("p4xc-audio","UNQUALIFIED AEC_GUARD_CONFIG guard_ms=%u afe_delay_samples=1024 rearm_ms=%u",guard,rearm);
}
void aec_init([[maybe_unused]] unsigned chunk,[[maybe_unused]] unsigned delay,[[maybe_unused]] int internal,[[maybe_unused]] int psram){
    ESP_LOGI("p4xc-aec","UNQUALIFIED esp-sr=2.4.7 AFE_AEC FD_LOW_COST filter=4 MR=MIC1,MIC3 chunk=%u adapter_delay=%u internal_delta=%d psram_delta=%d NS=0 AGC=0",chunk,delay,internal,psram);
}
constexpr AudioHw profile(){
    AudioHw value;
    value.codec=CodecKind::Es8311Es7210;
    value.analog_gain_error="Analog gain requires P4 ES7210";
    value.reference_gain_error="Reference gain requires P4 AEC";
    value.codec_log_tag="p4xc-audio";
    value.tdm_clock_description="RX=stereo32 TX=data16/slot32 BCLK=1024000 WS=16000";
    value.log_paired_clocks=paired_clocks;value.log_codec_summary=codec_summary;
    value.log_codec_probe=codec_probe;value.log_aec_guard=aec_guard;value.log_aec_init=aec_init;
    value.dac_i2c_address=0x18;value.adc_i2c_address=0x40;
    value.pa={53,true}; // No board or codec rail; never drive hosted's GPIO54.
    // TDM stays enabled with AEC off. RX owns external 256fs; paired TX is
    // a slave with internal 512fs and no MCLK pin, as proven in the spike.
    value.rx={0,12,10,11,32,32,32,I2sSlot::Stereo,{16000,256,I2sRole::Master,13,true},
              CONFIG_DG_SPEAKER_DMA_DESCRIPTORS,CONFIG_DG_SPEAKER_DMA_FRAMES,true};
    // tx.clock.role describes the paired result: allocation reads only RX
    // role, then IDF forces the second-initialized TX channel to Slave.
    value.tx={0,12,10,9,16,32,32,I2sSlot::Stereo,{16000,512,I2sRole::Slave,-1,false},
              CONFIG_DG_SPEAKER_DMA_DESCRIPTORS,CONFIG_DG_SPEAKER_DMA_FRAMES,true};
    value.rx.selected_slot=I2sSlot::Left;value.tx.selected_slot=I2sSlot::Stereo;
    value.microphones={AudioSlot{0,16},AudioSlot{1,16},AudioSlot{0,0},AudioSlot{}};
    // PA gain 0 matches the spike's 5.0V/3.3V vendor defaults. MIC1 gain
    // remains an EchoEar placeholder pending product PCM/STT measurement.
    // Expected REG44 is an independent measured table value, never inferred
    // from no_dac_reference (spike readback REG0D=01 REG44=58 MIC1=1a).
    // MIC4 is deliberately unselected (S3 AEC profile); the spike used mask
    // 0xF. Reconfirm MIC3 word0-low under mask 7 before step-15 reference use.
    // registers.mclk_multiple is descriptive for these pinned vendors: their
    // set_fs ignores that field and uses the codec config default mclk_div=256.
    value.registers={false,true,-1,0,7,30,32,256,-6,{0x0d,0x01},{0x44,0x58},{0x43,0x1a}};
    value.aec_reference=true;value.analog_gain_control=true;
    value.microphone_limiter=true;value.adc_clip_counting=true;
    value.default_output_gain_db=-6;
    value.paired_clock_limits={4095000,4097000,8190000,8194000,1023500,1024500,6};
    return value;
}
constexpr auto tdm=profile();
}
const AudioHw& audio_hw(bool){return tdm;}
}
