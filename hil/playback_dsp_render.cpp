// Host rendering uses the production pure DSP; fixed WAV payloads enter as PCM16.
#include "dg_audio/playback_dsp.h"
#include "sdkconfig.h"
#include <algorithm>
#include <cmath>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>
#include <memory>
#include <string>
int main(int argc,char** argv) {
    bool sequence=argc>1 && std::string(argv[1])=="--sequence";
    if(!sequence && argc!=5 && argc!=7) return 2;
    if(sequence && (argc<6 || argc%2)) return 2;
    if(sequence){--argc;++argv;}

    dg_audio::SpeechLevelConfig c;c.target_dbfs=std::stof(argv[1]);
    c.seed_dbfs=CONFIG_DG_PLAYBACK_SEED_DBFS_X10/10.f;
    c.max_gain_db=CONFIG_DG_PLAYBACK_MAX_GAIN_DB;c.activity_dbfs=CONFIG_DG_PLAYBACK_ACTIVITY_DBFS;
    c.level_ms=CONFIG_DG_PLAYBACK_LEVEL_MS;c.attack_ms=CONFIG_DG_PLAYBACK_ATTACK_MS;
    c.release_ms=CONFIG_DG_PLAYBACK_RELEASE_MS;c.lookahead_ms=CONFIG_DG_PLAYBACK_LOOKAHEAD_MS;
    c.limiter_release_ms=CONFIG_DG_PLAYBACK_LIMITER_RELEASE_MS;
    c.compression_ratio=CONFIG_DG_PLAYBACK_COMPRESSION_RATIO;
    c.compression_threshold_dbfs=CONFIG_DG_PLAYBACK_COMPRESSION_THRESHOLD_DBFS;
    if(!sequence && argc==7) {c.compression_ratio=std::stof(argv[5]);c.compression_threshold_dbfs=std::stof(argv[6]);}
    std::unique_ptr<dg_audio::PlaybackDsp> instance;
    if(c.target_dbfs==0) instance=std::make_unique<dg_audio::PlaybackDsp>(.33f,std::stof(argv[2])/100,CONFIG_DG_PLAYBACK_HIGHPASS_HZ);
    else instance=std::make_unique<dg_audio::PlaybackDsp>(std::stof(argv[2])/100,CONFIG_DG_PLAYBACK_HIGHPASS_HZ,c);
    auto& dsp=*instance;
    for(int input=3;input<(sequence?argc:5);input+=2) {
        if(input>3)dsp.reset();
        std::ifstream in(argv[input],std::ios::binary);
        std::vector<char> bytes((std::istreambuf_iterator<char>(in)),{});
        if(bytes.empty() || bytes.size()%2) return 3;
        std::vector<int16_t> out(bytes.size()/2+dsp.latency_samples());
        for(size_t i=0;i<bytes.size()/2;++i)out[i]=int16_t(uint8_t(bytes[2*i]) | (uint16_t(uint8_t(bytes[2*i+1]))<<8));
        dsp.process(out.data(),out.size());
        std::ofstream file(argv[input+1],std::ios::binary);
        file.write(reinterpret_cast<const char*>(out.data()+dsp.latency_samples()),bytes.size());
        if(!sequence || input+2>=argc)std::cout<<"{\"active_samples\":"<<dsp.active_samples()<<",\"limited_samples\":"<<dsp.engagements()
                 <<",\"peak_exceedances\":"<<dsp.peak_exceedances()<<",\"latency_samples\":"<<dsp.latency_samples()
                 <<",\"resets\":"<<dsp.resets()<<",\"normalizer_gain\":"<<dsp.normalizer_gain()<<"}\n";
    }
}
