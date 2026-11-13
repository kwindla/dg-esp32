#pragma once
#include <cmath>
namespace dg_audio {
// Output-domain PCM16 levels. Smooth knee, unity below knee, asymptotic ceiling.
// Scale back before digital gain so even the largest supported gain is bounded.
constexpr float mic_knee = 8192.0f;
constexpr float mic_ceiling = 16384.0f;
inline float protect_microphone(float filtered, float gain, bool& limited) {
    const float knee=mic_knee/gain, width=(mic_ceiling-mic_knee)/gain;
    const float excess=std::fabs(filtered)-knee;
    limited=excess>0;
    if(!limited)return filtered;
    return std::copysign(knee+width*(excess/(width+excess)),filtered);
}
}
