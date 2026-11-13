#include "dg_duo/install.h"
#include "check.h"
#include <cstring>
#include <stdexcept>
#include <vector>
using namespace dg_duo;
namespace {
struct Fake:InstallIo {
    Error prep=Error::None,admission=Error::None,fetch_error=Error::None;
    Error inspection[96]{};bool pins[96]{},durable[96]{};unsigned restores=0,fetches=0,pin_calls=0;
    int fail_pin=-1,throw_restore=-1,fail_restore=-1;bool bogus_fetch=false;std::vector<unsigned> events;
    const DuoAsset* assets;
    explicit Fake(const DuoAsset* a):assets(a){}
    unsigned index(const DuoAsset& a){return unsigned(&a-assets);}
    Error prepare()override{return prep;}
    Error admit(const DuoAsset*,size_t)override{return admission;}
    Error inspect(const DuoAsset& a)override{return inspection[index(a)];}
    Error restore(const DuoAsset& a)override{auto i=index(a);if(int(i)==throw_restore)throw std::runtime_error("test");
        if(int(i)==fail_restore)return Error::Hash;
        ++restores;events.push_back(100+i);return Error::None;}
    Error fetch(const DuoAsset& a,const char* origin)override{CHECK(origin && *origin);auto i=index(a);events.push_back(200+i);++fetches;
        if(fetch_error!=Error::None)return fetch_error;
        if(!bogus_fetch){durable[i]=true;inspection[i]=Error::None;}return Error::None;}
    Error pin(const DuoAsset& a)override{auto i=index(a);++pin_calls;if(int(i)==fail_pin)return Error::InUse;CHECK(!pins[i]);pins[i]=true;return Error::None;}
    void unpin(const DuoAsset& a)noexcept override{auto i=index(a);CHECK(pins[i]);pins[i]=false;}
    void empty(){for(bool pin:pins)CHECK(!pin);}
};
}
int main(){
    DuoAsset assets[96]{};auto n=install_assets(assets,96);CHECK(n==31);CHECK(install_assets(assets,2)==0);n=install_assets(assets,96);
    for(unsigned i=0;i<n;++i){CHECK(std::strlen(assets[i].hash)==64);for(unsigned j=0;j<i;++j)CHECK(std::strcmp(assets[i].hash,assets[j].hash));}
    {Fake io(assets);auto r=install(io,assets,n,"");CHECK(r.error==Error::None && r.pinned==31 && r.restored==31 && !io.fetches);}
    {Fake io(assets);io.inspection[0]=Error::NotLoaded;io.inspection[5]=Error::NotLoaded;
     auto r=install(io,assets,n,"http://fixture:7874");CHECK(r.error==Error::None && r.fetched==2 && r.restored==29 && r.pinned==31);
     CHECK(io.events[29]==200 && io.events[31]==205);CHECK(io.durable[0] && io.durable[5]);}
    {Fake io(assets);io.inspection[12]=Error::NotLoaded;auto r=install(io,assets,n,"");CHECK(r.missing_origin && r.error==Error::NotLoaded && !r.pinned && !io.fetches);io.empty();}
    for(auto e:{Error::Hash,Error::InvalidDgi,Error::StorageUnavailable}){Fake io(assets);io.inspection[12]=e;auto r=install(io,assets,n,"http://fixture:7874");CHECK(r.error==e && !r.pinned && !io.fetches);io.empty();}
    for(auto e:{Error::Fetch,Error::Hash,Error::FlashFull,Error::StorageUnavailable}){Fake io(assets);io.inspection[12]=Error::NotLoaded;io.fetch_error=e;
     auto r=install(io,assets,n,"http://fixture:7874");CHECK(r.error==e && !r.pinned);io.empty();}
    {Fake io(assets);io.prep=Error::StorageUnavailable;CHECK(install(io,assets,n,"").error==io.prep);CHECK(!io.restores);io.empty();}
    {Fake io(assets);io.admission=Error::FlashFull;CHECK(install(io,assets,n,"").error==io.admission);CHECK(!io.restores);io.empty();}
    {Fake io(assets);io.fail_pin=10;CHECK(install(io,assets,n,"").error==Error::InUse);io.empty();}
    {Fake io(assets);io.fail_restore=10;CHECK(install(io,assets,n,"").error==Error::Hash);io.empty();}
    {Fake io(assets);io.inspection[12]=Error::NotLoaded;io.bogus_fetch=true;
     CHECK(install(io,assets,n,"http://fixture:7874").error==Error::NotLoaded);io.empty();}
    {Fake io(assets);io.inspection[12]=Error::NotLoaded;io.fail_pin=12;
     CHECK(install(io,assets,n,"http://fixture:7874").error==Error::InUse);io.empty();CHECK(io.durable[12]);}
    {Fake io(assets);io.throw_restore=10;try{install(io,assets,n,"");CHECK(false);}catch(const std::runtime_error&){}io.empty();}
    {Fake io(assets);CHECK(install(io,assets,97,"").error==Error::Malformed);}
}
