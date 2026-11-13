#include "dg_board/audio_hw.h"
namespace dg_board {
namespace {
void paired(unsigned long,unsigned long,unsigned long,unsigned long){}
void summary(){}
void probe(bool,bool,bool){}
void guard(unsigned,unsigned){}
void aec(unsigned,unsigned,int,int){}
constexpr AudioHw profile(){
    AudioHw value;
    value.analog_gain_error="Analog gain unavailable on null audio";
    value.reference_gain_error="Reference gain unavailable on null audio";
    value.codec_log_tag="null-audio";value.tdm_clock_description="No hardware clocks";
    value.log_paired_clocks=paired;value.log_codec_summary=summary;value.log_codec_probe=probe;
    value.log_aec_guard=guard;value.log_aec_init=aec;
    return value;
}
constexpr auto null=profile();
}
// The silence/discard backend remains independently linkable without hardware.
const AudioHw& audio_hw(bool){return null;}
}
