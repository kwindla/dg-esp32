#pragma once
#include <cstdint>
#include <stdexcept>
#include <string_view>

namespace dg_audio {
constexpr bool valid_reference_gain(int db) { return db >= 0 && db <= 33 && db % 3 == 0; }
inline unsigned parse_reference_gain(std::string_view text) {
    // Consume the entire argument: reject overflow, fractions and trailing tokens.
    while(!text.empty() && text.front()==' ')text.remove_prefix(1);
    while(!text.empty() && text.back()==' ')text.remove_suffix(1);
    if(text.empty() || text.size()>2)throw std::invalid_argument("Reference gain: 0..33 dB by 3");
    unsigned db=0;
    for(char c:text) {
        if(c<'0' || c>'9')throw std::invalid_argument("Reference gain integer required");
        db=db*10+unsigned(c-'0');
    }
    if(!valid_reference_gain(db))throw std::invalid_argument("Reference gain: 0..33 dB by 3");
    return db;
}
constexpr unsigned reference_gain_register(unsigned db) { return 0x10 | (db/3); }
// Power gain, Q1000, rounded from 10^(dB/10); no floating point in capture.
constexpr unsigned reference_power_gain(unsigned db) {
    constexpr unsigned ladder[]={1000,1995,3981,7943,15849,31623,63096,125893,251189,501187,1000000,1995262};
    return valid_reference_gain(db) ? ladder[db/3] : 0;
}
// Diagnostic threshold: MIC3 power must exceed 1 ppm of accepted playback
// power at 0 dB, scaled by analog gain. Bench reports measured loopback ratios;
// this is deliberately not an acceptance gate or a runtime fallback.
constexpr bool reference_power_low(uint64_t sum, unsigned n, unsigned playback, unsigned db) {
    return sum < (uint64_t(n)*playback*reference_power_gain(db)+999999999ULL)/1000000000ULL;
}
}
