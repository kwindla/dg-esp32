#pragma once
#include <cstddef>
#include <stdexcept>
#include "app.h"
namespace dg_display {
inline void session_end() {boot_fake::session=false;++boot_fake::ended;}
inline void standalone_assets(const char* const*,size_t) {
 if(boot_fake::session)throw std::runtime_error("display session still active");
}
}
