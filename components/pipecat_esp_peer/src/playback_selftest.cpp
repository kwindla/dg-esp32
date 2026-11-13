#include "pipecat/esp_peer/codec_benchmark.h"
#include "opus.h"
#include "playout_pcm.h"
#include "playout_opus_fixture.h"
#include "playout_run_selftest.h"
#include <algorithm>
#include <stdexcept>
namespace pipecat::esp_peer {
nlohmann::json playback_selftest() {
    OpusDecoder decoder,reference;PlayoutPcm renderer;int16_t out[320],raw[320];
    auto require=[](bool ok,const char* message) {if(!ok)throw std::runtime_error(message);};
    auto packet=[] {Playout::Decision d;d.kind=Playout::Kind::Decode;
        d.payload=const_cast<uint8_t*>(playout_opus_fixture);d.size=sizeof(playout_opus_fixture);return d;};
    auto d=packet();require(renderer.render(d,decoder,out).decoded,"fixture decode");
    reference.decode(d.payload,d.size,raw,320,false);
    static constexpr float end[]={1,1,2.f/3,1.f/3,0};
    for(unsigned run=1;run<=5;++run) {
        d.kind=Playout::Kind::Plc;d.missing_run=run;
        require(!renderer.render(d,decoder,out).error,"PLC decode");
        reference.decode(nullptr,0,raw,320,true);
        float to=end[run-1],from=run==1?1:end[run-2];
        for(unsigned i=0;i<320;++i)require(out[i]==int16_t(raw[i]*(from+(to-from)*(i+1)/320.f)),"PLC envelope");
    }
    uint8_t bad=255;d=packet();d.payload=&bad;d.size=1;
    require(renderer.render(d,decoder,out).error && !renderer.valid(),"failed decode recovery");
    d.kind=Playout::Kind::Insert;renderer.render(d,decoder,out);
    require(std::all_of(out,out+320,[](auto s){return s==0;}),"invalid expansion");
    d.kind=Playout::Kind::Zero;d.missing_run=25;renderer.render(d,decoder,out);
    d=packet();require(renderer.render(d,decoder,out).decoded,"long mute recovery");
    reference.reset();reference.decode(d.payload,d.size,raw,320,false);
    require(std::equal(out,out+320,raw),"long mute decoder reset");
    decoder.reset();d.payload=const_cast<uint8_t*>(playout_quiet_fixture);d.size=sizeof(playout_quiet_fixture);
    require(renderer.render(d,decoder,out).decoded,"inactive fixture");
    require(std::any_of(out,out+320,[](auto s){return s!=0;}) && !PlayoutPcm::active(out),"inactive nonzero PCM");
    d.kind=Playout::Kind::Insert;renderer.render(d,decoder,out);
    require(std::all_of(out,out+320,[](auto s){return s==0;}),"inactive expansion");
    return {{"passed",true},{"codec","vendor OpusDecoder"},{"fade_samples_checked",1600},
        {"bad_decode",true},{"long_mute_reset",true},{"inactive_nonzero_expansion",true},
        {"missing_run_accounting_cases",playout_run_selftest()}};
}
}
