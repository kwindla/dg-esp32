// Bundle metadata reads the actual P4 table instead of copying its defaults.
#include "dg_board/audio_hw.h"
#include <cstdio>
int main(){
    const auto& hw=dg_board::audio_hw();
    std::printf("{\"analog_db\":%u,\"playback_volume_db\":%d}\n",
        hw.registers.analog_gain_db,hw.default_output_gain_db);
}
