// Host-only observer, compiled with -fno-access-control. The unchanged product
// PlaybackDsp does every filter/compressor/activity operation. Read the stored
// pre-limiter sample and remove its current normalizer gain, exactly the basis
// used by PlaybackDsp's effective-source estimator. No firmware instrumentation.
#include "dg_audio/playback_dsp.h"
#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <vector>
int main(int argc,char** argv){
    if(argc!=16)return 2;
    try {
        dg_audio::SpeechLevelConfig c;
        float* fields[]={&c.target_dbfs,&c.seed_dbfs,&c.max_gain_db,&c.activity_dbfs,&c.level_ms,
            &c.attack_ms,&c.release_ms,&c.lookahead_ms,&c.limiter_release_ms,&c.compression_ratio,&c.compression_threshold_dbfs};
        for(unsigned i=0;i<11;++i)*fields[i]=std::stof(argv[i+3]);
        dg_audio::PlaybackDsp dsp(std::stof(argv[14])/100,std::stof(argv[15]),c);
        std::ifstream input(argv[1],std::ios::binary);
        std::vector<char> bytes((std::istreambuf_iterator<char>(input)),{});
        if(bytes.empty()||bytes.size()%2)return 3;
        const size_t count=bytes.size()/2,latency=dsp.latency_samples();
        std::vector<int16_t> rendered;rendered.reserve(count);
        std::vector<uint8_t> activity;activity.reserve(count);
        double power=0;uint64_t active=0,clips=0,output_clips=0;
        for(size_t i=0;i<count+latency;++i){
            int16_t x=i<count?int16_t(uint8_t(bytes[2*i])|(uint16_t(uint8_t(bytes[2*i+1]))<<8)):0;
            if(i<count&&std::abs(int(x))>=32767)++clips;
            float y=dsp.sample(x/32768.f);
            if(i<count)activity.push_back(dsp.active_);
            if(i<count&&dsp.active_){
                float source=dsp.delay_[(dsp.delay_at_+latency-1)%latency]/dsp.normalizer_gain();
                power+=double(source)*source;++active;
            }
            if(i>=latency){
                if(std::abs(y)>=1)++output_clips;
                rendered.push_back(int16_t(std::lround(y*32767)));
            }
        }
        if(!active||power<=0)return 4;
        std::ofstream output(argv[2],std::ios::binary);output.write(reinterpret_cast<const char*>(rendered.data()),rendered.size()*2);
        if(!output)return 5;
        std::ofstream mask(std::string(argv[2])+".active",std::ios::binary);
        mask.write(reinterpret_cast<const char*>(activity.data()),activity.size());if(!mask)return 5;
        std::cout<<std::setprecision(15)<<"{\"samples\":"<<count<<",\"active_samples\":"<<active
            <<",\"effective_power_sum\":"<<power<<",\"active_effective_dbfs\":"<<10*std::log10(power/active)
            <<",\"source_clips\":"<<clips<<",\"output_clips\":"<<output_clips
            <<",\"limited_samples\":"<<dsp.engagements()<<"}\n";
    }catch(const std::exception&){return 6;}
}
