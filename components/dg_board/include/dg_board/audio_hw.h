#pragma once
#include <array>
#include <cstdint>
namespace dg_board {
enum class CodecKind {Es8311Es7210,DirectI2s,Null};
enum class I2sRole {Master,Slave};
enum class I2sSlot {Left,Right,Stereo};
struct GpioLevel {int gpio=-1;bool active_high=false;};
struct I2sClock {
    unsigned sample_rate_hz=0,mclk_multiple=0;
    I2sRole role=I2sRole::Master;
    int mclk_gpio=-1;
    bool owns_external_mclk=false;
};
struct I2sStream {
    int port=-1,bclk_gpio=-1,ws_gpio=-1,data_gpio=-1;
    unsigned data_bits=0,slot_bits=0,ws_width=0;
    I2sSlot slot=I2sSlot::Stereo;
    I2sClock clock;
    unsigned dma_descriptors=0,dma_frames=0;
    bool auto_clear=false;
    // PCM selection can differ from the stereo wire/DMA slot configuration.
    // RX describes microphones[0] (the map is authoritative); TX selects
    // the speaker packing and measurement slot.
    I2sSlot selected_slot=I2sSlot::Stereo;
};
// Position of a signed PCM16 sample within the DMA frame's 32-bit words.
struct AudioSlot {int word=-1;unsigned shift=0;};
struct RegisterAssert {uint8_t reg=0,value=0;};
struct CodecProfile {
    bool no_dac_reference=false,use_mclk=false;
    int codec_pa_gpio=-1,hardware_pa_gain_db=0;
    unsigned mic_mask=0,analog_gain_db=0,adc_format_bits=0,mclk_multiple=0;
    int dac_volume_db=0;
    RegisterAssert reg0d,reg44,mic1_gain;
};
struct PairedClockLimits {
    unsigned rx_mclk_min=0,rx_mclk_max=0,tx_mclk_min=0,tx_mclk_max=0;
    unsigned bclk_min=0,bclk_max=0,tx_mclk_bclk_min_ratio=0;
};
struct AudioHw {
    CodecKind codec=CodecKind::Null;
    int dac_i2c_address=-1,adc_i2c_address=-1;
    GpioLevel board_rail,codec_rail,pa;
    I2sStream rx,tx;
    std::array<AudioSlot,4> microphones{};
    CodecProfile registers;
    bool aec_reference=false,analog_gain_control=false;
    bool microphone_limiter=false,adc_clip_counting=false;
    int default_output_gain_db=0;
    const char* analog_gain_error=nullptr;
    const char* reference_gain_error=nullptr;
    const char* codec_log_tag=nullptr;
    const char* tdm_clock_description=nullptr;
    PairedClockLimits paired_clock_limits;
    void (*log_paired_clocks)(unsigned long,unsigned long,unsigned long,unsigned long)=nullptr;
    void (*log_codec_summary)()=nullptr;
    void (*log_codec_probe)(bool,bool,bool)=nullptr;
    void (*log_aec_guard)(unsigned,unsigned)=nullptr;
    void (*log_aec_init)(unsigned,unsigned,int,int)=nullptr;
};
// Select the existing stereo16 / AEC stereo32 EchoEar profiles,
// using the single authoritative AEC Kconfig input.
const AudioHw& audio_hw(bool aec=false);
}
