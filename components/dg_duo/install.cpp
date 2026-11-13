#include "dg_duo/install.h"
#include "duo_pack.gen.h"
#include "dg_display/demo_assets.h"
#include <cstring>
namespace dg_duo {
size_t install_assets(DuoAsset* out,size_t cap)noexcept {
    size_t count=0;bool full=false;
    auto add=[&](const char* hash,unsigned bytes){
        for(size_t i=0;i<count;++i)if(!std::strcmp(hash,out[i].hash))return;
        if(count==cap){full=true;return;}out[count++]={hash,bytes};
    };
    for(const auto& a:dg_display::demo_assets)add(a.hash,a.bytes);
    for(const auto* p:Packs){add(p->faceplate.hash,p->faceplate.bytes);
        for(unsigned i=0;i<p->eye_count;++i)add(p->eyes[i].hash,p->eyes[i].bytes);
        for(unsigned i=0;i<p->mouth_count;++i)add(p->mouths[i].hash,p->mouths[i].bytes);
    }
    return full?0:count;
}
InstallResult install(InstallIo& io,const DuoAsset* assets,size_t count,const char* origin) {
    InstallResult r;bool pinned[96]{},missing[96]{};
    auto unwind=[&]{for(size_t i=0;i<count;++i)if(pinned[i])io.unpin(assets[i]);r.pinned=0;};
    if(!assets || !count || count>96){r.error=Error::Malformed;return r;}
    try {
        r.error=io.prepare();if(r.error!=Error::None)return r;
        r.error=io.admit(assets,count);if(r.error!=Error::None)return r;
        // Complete restore before the first HTTP request. Corrupt files fail
        // closed: they are reported, never silently erased or reformatted.
        for(size_t i=0;i<count;++i){
            r.error=io.inspect(assets[i]);
            if(r.error==Error::NotLoaded){missing[i]=true;continue;}
            if(r.error!=Error::None){unwind();return r;}
            r.error=io.restore(assets[i]);if(r.error!=Error::None){unwind();return r;}
            r.error=io.pin(assets[i]);if(r.error!=Error::None){unwind();return r;}
            pinned[i]=true;++r.pinned;++r.restored;
        }
        for(size_t i=0;i<count;++i)if(missing[i]){
            if(!origin || !*origin){r.error=Error::NotLoaded;r.missing_origin=true;unwind();return r;}
            r.error=io.fetch(assets[i],origin);if(r.error!=Error::None){unwind();return r;}
            // Require a verified persisted catalog entry and resident decoder.
            r.error=io.inspect(assets[i]);if(r.error==Error::None)r.error=io.restore(assets[i]);
            if(r.error==Error::None)r.error=io.pin(assets[i]);
            if(r.error!=Error::None){unwind();return r;}
            pinned[i]=true;++r.pinned;++r.fetched;
        }
    }catch(...){unwind();throw;}
    r.error=Error::None;return r;
}
}
