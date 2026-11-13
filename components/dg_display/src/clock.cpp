#include "dg_display/clock.h"
namespace dg_display {
Presentation FakeClock::map(uint32_t t,uint32_t requested)const noexcept {
    if(requested!=epoch)return {MappingState::Unknown,0,uncertainty_us,epoch};
    if(!valid)return {MappingState::Stale,0,uncertainty_us,epoch};
    for(unsigned i=0;i<count;++i){const auto& s=spans[i];uint32_t distance=t-s.timestamp;int64_t d=distance<=0x7fffffffu?int64_t(distance):int64_t(distance)-0x100000000ll;
        if(s.kind==Kind::Insert || d<0 || uint64_t(d)>=s.length)continue;
        if(s.kind==Kind::Drop || s.kind==Kind::Trim)return {MappingState::Skipped,0,uncertainty_us,epoch};
        return {MappingState::Valid,s.speaker_us+d*1000000/48000,uncertainty_us,epoch};
    }
    return {MappingState::Unknown,0,uncertainty_us,epoch};
}
}
