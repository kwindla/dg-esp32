#pragma once
#include <cstring>
namespace dg {
enum class ConsoleSupport {Available,Board,AecDisabled,CacheDisabled};
inline ConsoleSupport console_support(const char* command,bool analog_gain,bool aec,bool cache,bool cache_hardware) {
    if(!std::strcmp(command,"mic-analog-gain") && !analog_gain)return ConsoleSupport::Board;
    if(!std::strcmp(command,"cache-counters"))
        return !cache?ConsoleSupport::CacheDisabled:!cache_hardware?ConsoleSupport::Board:ConsoleSupport::Available;
    if(!aec && ((!std::strncmp(command,"aec-",4) && std::strcmp(command,"aec-clock")) ||
        !std::strcmp(command,"dump-aec")))return ConsoleSupport::AecDisabled;
    // POWER snapshots and touch status/UI controls remain useful with absent
    // hardware, including the existing Waveshare no-battery/no-touch paths.
    return ConsoleSupport::Available;
}
}
