#pragma once
#include "dg_display/duo_runtime.h"
#include <cstddef>
namespace dg_duo {
using dg_display::DuoAsset;
using dg_display::Error;
// All callbacks run on the idle boot owner. The display store owns physical IO.
struct InstallIo {
    virtual ~InstallIo()=default;
    virtual Error prepare()=0;
    virtual Error inspect(const DuoAsset&)=0;
    virtual Error restore(const DuoAsset&)=0;
    virtual Error fetch(const DuoAsset&,const char* origin)=0;
    virtual Error pin(const DuoAsset&)=0;
    virtual void unpin(const DuoAsset&)noexcept=0;
    virtual Error admit(const DuoAsset*,size_t)=0;
};
struct InstallResult {Error error=Error::None;unsigned restored=0,fetched=0,pinned=0;bool missing_origin=false;};
InstallResult install(InstallIo&,const DuoAsset*,size_t,const char* origin);
// Status screens first, followed by unique hashes from both generated packs.
size_t install_assets(DuoAsset*,size_t capacity)noexcept;
}
