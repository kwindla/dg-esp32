#pragma once
#if defined(ESP_PLATFORM) || __has_include("sdkconfig.h")
#include "sdkconfig.h"
#endif

namespace dg_audio::aec_config {
// The legacy symbols remain authoritative for both boards. Inactive timing
// and gain values are zero; enabled configurations use only Kconfig values.
#if CONFIG_DG_ECHOEAR_AEC
inline constexpr bool enabled=true;
inline constexpr unsigned ref_gain_db=CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB;
inline constexpr unsigned guard_ms=CONFIG_DG_ECHOEAR_AEC_GUARD_MS;
inline constexpr unsigned rearm_ms=CONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS;
#else
inline constexpr bool enabled=false;
inline constexpr unsigned ref_gain_db=0;
inline constexpr unsigned guard_ms=0;
inline constexpr unsigned rearm_ms=0;
#endif
}
