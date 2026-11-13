#pragma once
#include "playout.h"
#include <algorithm>
#include <cstdint>
namespace pipecat::esp_peer {
// Consumer-owned codec recovery state, shared by production and decoder tests.
class PlayoutPcm {
    int16_t prior_[320]{};
    bool valid_=false, active_=false, long_muted_=false;
public:
    bool valid() const {return valid_;}
    void invalidate() {valid_=active_=false;}
    // RMS >= 32 PCM units (about -60 dBFS). Comfort noise below this floor
    // expands to exact zero; the encoded representation is never consulted.
    static bool active(const int16_t* pcm) {
        uint64_t sum=0;for(unsigned i=0;i<320;++i)sum+=int64_t(pcm[i])*pcm[i];
        return sum>=uint64_t(320)*32*32;
    }
    struct Result {bool decoded=false, error=false;};
    template<class Codec> Result render(const Playout::Decision& d,Codec& codec,int16_t* out) {
        std::fill_n(out,320,0);Result result;
        try {
            if(d.reset_decoder) {invalidate();long_muted_=false;codec.reset();}
            if(d.kind==Playout::Kind::Insert) {
                if(valid_ && active_)for(unsigned i=0;i<320;++i)out[i]=int32_t(prior_[i])*3/4;
            } else if(d.kind==Playout::Kind::Decode) {
                if(long_muted_) {invalidate();codec.reset();long_muted_=false;}
                codec.decode(d.payload,d.size,out,320,false);result.decoded=true;
            } else if(d.kind==Playout::Kind::Plc) {
                codec.decode(nullptr,0,out,320,true);
                static constexpr float end[]={1,1,2.f/3,1.f/3,0};
                unsigned run=d.missing_run;float to=end[run-1],from=run==1?1:end[run-2];
                for(unsigned i=0;i<320;++i)out[i]*=from+(to-from)*(i+1)/320.f;
            }
        } catch(...) {result.error=true;invalidate();std::fill_n(out,320,0);}
        if(d.kind==Playout::Kind::Zero && d.missing_run>=25)long_muted_=true;
        if(result.decoded || d.kind==Playout::Kind::Insert) {
            std::copy_n(out,320,prior_);valid_=true;active_=active(out);
        }
        return result;
    }
};
}
