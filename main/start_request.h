#pragma once
#include "dg_audio/capability.h"
#include <nlohmann/json.hpp>
namespace dg {
inline nlohmann::json start_request(dg_audio::Capability audio) {
    return {{"transport","webrtc"},{"body",{{"audio",{{"v",1},{"aec",audio.aec},
        {"ref_gain_db",audio.ref_gain_db},{"guard_ms",audio.guard_ms}}}}}};
}
}
