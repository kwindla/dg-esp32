// Independent allocator census of FOUR simultaneous full DOMs plus a dump,
// a deliberately larger cohort than the transport -> SDK -> callback path.
#include "message_admission.h"
#include <nlohmann/json.hpp>
#include <cstdlib>
#include <cstddef>
#include <new>
#include <iostream>
#include <string>
namespace {
bool tracking=false;size_t live=0,peak=0;
struct alignas(std::max_align_t) Header {size_t bytes;bool tracked;};
}
void* operator new(size_t n) {
    auto* p=static_cast<Header*>(std::malloc(sizeof(Header)+n));if(!p)throw std::bad_alloc();
    p->bytes=n;p->tracked=tracking;if(tracking){live+=n+16;peak=std::max(peak,live);}return p+1;
}
void operator delete(void* ptr) noexcept {
    if(!ptr)return;auto* p=static_cast<Header*>(ptr)-1;
    if(p->tracked)live-=p->bytes+16;std::free(p);
}
void operator delete(void* ptr,size_t) noexcept {::operator delete(ptr);}
int main() {
    using pipecat::esp_peer::MessageAdmission;
    std::string many="{\"data\":{";
    for(int i=0;i<12;++i){if(i)many+=',';many+='"'+std::to_string(i)+"\":\"x\"";}many+="}}";
    std::string deep="0";for(int i=0;i<16;++i)deep="{\"a\":"+deep+"}";
    const std::string cases[]={"{\"data\":\""+std::string(1800,'x')+"\"}",many,deep,
        "{\"data\":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31]}",
        "{\"a\":{\"b\":{\"c\":{\"d\":{\"e\":{\"f\":{\"g\":{\"h\":{\"i\":{\"j\":\"x\"}}}}}}}}}}"};
    for(const auto& wire:cases) {
        live=peak=0;tracking=true;
        {auto a=nlohmann::json::parse(wire);auto b=a;auto c=b;auto d=c;auto dump=d.dump();}
        tracking=false;
        const auto bound=MessageAdmission::reserve(wire.size(),MessageAdmission::nodes(wire));
        std::cout<<"wire="<<wire.size()<<" nodes="<<MessageAdmission::nodes(wire)<<" host_peak_including_16B_per_alloc="<<peak<<" bound="<<bound<<'\n';
        if(live || peak>bound || MessageAdmission::nodes(wire)>MessageAdmission::node_limit)return 1;
    }
}
