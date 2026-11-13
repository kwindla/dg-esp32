#include "dg_board/audio_hw.h"
#include "sdkconfig.h"
#include "esp_log.h"
namespace dg_board {
namespace {
void aec_guard([[maybe_unused]] unsigned guard,[[maybe_unused]] unsigned rearm){
    ESP_LOGI("dg-audio","AEC_GUARD_CONFIG guard_ms=%u afe_delay_samples=1024 rearm_ms=%u",guard,rearm);
}
void paired_clocks(unsigned long,unsigned long,unsigned long,unsigned long) {}
void codec_summary() {}
void codec_probe(bool,bool,bool) {}
void aec_init(unsigned,unsigned,int,int) {}
constexpr AudioHw profile(){
    AudioHw value;
    value.codec=CodecKind::DirectI2s;value.log_aec_guard=aec_guard;
    value.log_paired_clocks=paired_clocks;value.log_codec_summary=codec_summary;
    value.log_codec_probe=codec_probe;value.log_aec_init=aec_init;
    value.analog_gain_error="Analog gain is EchoEar only";
    value.reference_gain_error="Reference gain requires EchoEar AEC";
    value.codec_log_tag="waveshare146-audio";
    value.tdm_clock_description="Direct I2S; no TDM reference";
    value.rx={0,15,2,39,32,32,32,I2sSlot::Stereo,{16000,256,I2sRole::Master,-1,false},
              CONFIG_DG_MIC_DMA_DESCRIPTORS,CONFIG_DG_MIC_DMA_FRAMES,false};
    value.rx.selected_slot=I2sSlot::Right;
    value.tx={1,48,38,47,16,16,16,I2sSlot::Stereo,{16000,256,I2sRole::Master,-1,false},
              CONFIG_DG_SPEAKER_DMA_DESCRIPTORS,CONFIG_DG_SPEAKER_DMA_FRAMES,true};
    value.microphones={AudioSlot{1,16},AudioSlot{},AudioSlot{},AudioSlot{}};
    return value;
}
constexpr auto direct=profile();
}
const AudioHw& audio_hw(bool){return direct;}
}
