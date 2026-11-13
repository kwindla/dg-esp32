#include "dg_display/json_request.h"
#include <nlohmann/json.hpp>
#include <cstring>
#include <cstdlib>
#include <atomic>
#include <new>
#ifdef ESP_PLATFORM
#include "esp_heap_caps.h"
#endif
namespace dg_display {
namespace {
struct alignas(16) Header {size_t charge;};
std::atomic<size_t> parser_bytes{0};
constexpr size_t ParserBudget=48*1024;
template<class T> struct Allocator {
    using value_type=T;Allocator()=default;template<class U>Allocator(const Allocator<U>&){}
    T* allocate(size_t n){
        static_assert(alignof(T)<=16);if(n>(SIZE_MAX-32)/sizeof(T))throw std::bad_alloc();size_t bytes=n*sizeof(T)+sizeof(Header),charge=bytes+16;size_t old=parser_bytes.load();
        do{if(charge>ParserBudget || old>ParserBudget-charge)throw std::bad_alloc();}while(!parser_bytes.compare_exchange_weak(old,old+charge));
#ifdef ESP_PLATFORM
        auto* p=heap_caps_aligned_alloc(16,bytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
#else
        auto* p=std::malloc(bytes);
#endif
        if(!p){parser_bytes.fetch_sub(charge);throw std::bad_alloc();}auto* header=new(p)Header{charge};return reinterpret_cast<T*>(header+1);
    }
    void deallocate(T* p,size_t){auto* header=reinterpret_cast<Header*>(p)-1;size_t charge=header->charge;std::free(header);parser_bytes.fetch_sub(charge);}
    template<class U>bool operator==(const Allocator<U>&)const{return true;}
    template<class U>bool operator!=(const Allocator<U>&)const{return false;}
};
using JsonString=std::basic_string<char,std::char_traits<char>,Allocator<char>>;
using Json=nlohmann::basic_json<std::map,std::vector,JsonString,bool,int64_t,uint64_t,double,Allocator>;
uint32_t number(const Json& j,const char* key,uint32_t fallback=0){if(!j.contains(key))return fallback;const auto& v=j.at(key);if(!v.is_number_integer() || (v.is_number_integer() && v.get<double>()<0) || v.get<double>()>4294967295.0)throw 1;return v.get<uint32_t>();}
int16_t position(const Json& j,const char* key){if(!j.contains(key))return 0;auto& v=j.at(key);if(!v.is_number_integer() || v.get<double>()<-32768 || v.get<double>()>32767)throw 1;return v.get<int16_t>();}
void string(const Json& j,const char* key,char* out,size_t cap,bool required=true){if(!j.contains(key)){if(required)throw 1;return;}auto& v=j.at(key);if(!v.is_string())throw 1;auto& s=v.template get_ref<const JsonString&>();if(s.size()>=cap || s.find('\0')!=JsonString::npos)throw 1;std::memcpy(out,s.c_str(),s.size()+1);}
SceneOperation operation(const Json& j,const char* implicit=nullptr){SceneOperation op;if(!j.is_object())throw 1;char name[16]{};if(implicit)std::strncpy(name,implicit,15);else string(j,"op",name,sizeof(name));uint32_t layer=number(j,"layer",0);if(layer>=8)throw 1;op.layer=layer;
    if(!std::strcmp(name,"show")){op.kind=Operation::Show;string(j,"hash",op.hash,65);op.x=position(j,"x");op.y=position(j,"y");}
    else if(!std::strcmp(name,"hide"))op.kind=Operation::Hide;
    else if(!std::strcmp(name,"fill")){op.kind=Operation::Fill;uint32_t color=number(j,"color");if(color>65535)throw 1;op.color=color;}
    else throw 1;
    return op;
}
}
static Error parse_impl(const char* action,const char* input,size_t length,const Meta* defaults,Request& out,bool* matched=nullptr,Meta* correlation=nullptr) noexcept {
    if(matched)*matched=false;
    if(correlation)*correlation=Meta{};
    if(!action || !input || !length || length>4096)return Error::Malformed;
    unsigned depth=0,nodes=0;bool quoted=false,escape=false;for(size_t i=0;i<length;++i){char c=input[i];if(quoted){if(escape)escape=false;else if(c=='\\')escape=true;else if(c=='"')quoted=false;}else if(c=='"')quoted=true;else if(c=='{' || c=='['){if(++depth>8 || ++nodes>128)return Error::Malformed;}else if(c==',' || c==':'){if(++nodes>128)return Error::Malformed;}else if(c=='}' || c==']'){if(!depth)return Error::Malformed;--depth;}}if(depth || quoted)return Error::Malformed;
    try {
        Json envelope=Json::parse(input,input+length);if(!envelope.is_object())return Error::Malformed;
        char wire_action[16]{};
        const Json* body=&envelope;
        if(matched){
            char type[32]{};string(envelope,"t",type,sizeof(type));
            if(std::strncmp(type,"display.",8))return Error::Malformed;
            *matched=true;const size_t action_length=std::strlen(type+8);if(action_length>=sizeof(wire_action))return Error::Malformed;std::memcpy(wire_action,type+8,action_length+1);action=wire_action;
            body=&envelope.at("d");if(!body->is_object())return Error::Malformed;
            // Extract independently validated envelope fields before operation
            // validation, preserving correlation on malformed operations.
            if(correlation){
                try{correlation->sequence=number(*body,"seq");}catch(...){}
                try{const auto& e=body->at("epoch");if(e.is_number_integer() && e.get<double>()>=1 && e.get<double>()<=9007199254740991.0)correlation->epoch=e.get<uint64_t>();}catch(...){}
                try{string(*body,"id",correlation->id,sizeof(correlation->id));}catch(...){}
            }
            // Remote messages cannot bypass the configured asset origin.
            if(body->contains("any_host"))return Error::Malformed;
        }
        const Json& j=*body;Request q;if(defaults)q.meta=*defaults;else q.meta=Meta{};
        q.meta.version=number(j,"v",defaults?q.meta.version:0);if(j.contains("epoch")){auto& epoch=j.at("epoch");if(!epoch.is_number_integer() || epoch.get<double>()<1 || epoch.get<double>()>9007199254740991.0)throw 1;q.meta.epoch=epoch.get<uint64_t>();}q.meta.sequence=number(j,"seq",q.meta.sequence);string(j,"id",q.meta.id,33,!defaults);string(j,"utterance_id",q.utterance,33,false);
        if(j.contains("at")){const auto& at=j.at("at");if(!at.is_object())return Error::Malformed;char kind[16];string(at,"kind",kind,16);if(!std::strcmp(kind,"rtp"))q.time=TimeKind::Rtp;else if(!std::strcmp(kind,"arrival"))q.time=TimeKind::Arrival;else return Error::Malformed;q.timestamp=number(at,"timestamp");q.clock_epoch=number(at,"clock_epoch");string(at,"utterance_id",q.utterance,33,false);}
        if(!std::strcmp(action,"batch")){auto& ops=j.at("ops");if(!ops.is_array() || ops.empty() || ops.size()>8)return Error::Malformed;q.count=ops.size();for(unsigned i=0;i<q.count;++i)q.operations[i]=operation(ops.at(i));}
        else if(!std::strcmp(action,"show") || !std::strcmp(action,"hide") || !std::strcmp(action,"fill")){q.count=1;q.operations[0]=operation(j,action);}
        else if(!std::strcmp(action,"cancel")){q.kind=RequestKind::Cancel;string(j,"utterance_id",q.utterance,33);}
        else {
            if(!std::strcmp(action,"load") || !std::strcmp(action,"fetch"))q.kind=RequestKind::Load;
            else if(!std::strcmp(action,"preload"))q.kind=RequestKind::Preload;
            else if(!std::strcmp(action,"delete"))q.kind=RequestKind::Delete;
            else if(!std::strcmp(action,"persist"))q.kind=RequestKind::Persist;
            else return Error::Malformed;
            string(j,"hash",q.hash,65);if(q.kind==RequestKind::Load){string(j,"url",q.url,513);q.bytes=number(j,"bytes");if(j.contains("any_host")){if(!j.at("any_host").is_boolean())return Error::Malformed;q.any_host=j.at("any_host").get<bool>();}}
        }
        out=q;return Error::None;
    }catch(const std::bad_alloc&){return Error::RamBudget;}catch(...){return Error::Malformed;}
}
Error parse_request(const char* action,const char* input,size_t length,const Meta* defaults,Request& out) noexcept {
    return parse_impl(action,input,length,defaults,out);
}
Error parse_server_request(const char* input,size_t length,Request& out,bool& matched,Meta* correlation) noexcept {
    matched=false;
    if(correlation)*correlation=Meta{};
    // App server events have a fixed 1024-byte buffer, including NUL.
    if(length>1023)return Error::Malformed;
    return parse_impl("wire",input,length,nullptr,out,&matched,correlation);
}
}
