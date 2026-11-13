#pragma once
#include <cstdint>
namespace pipecat::esp_peer {
// libopus 1.5.2: 16 kHz mono, 20 ms, application AUDIO, 440 Hz / 8000 PCM units.
inline constexpr uint8_t playout_opus_fixture[]={72,130,181,3,108,158,153,172,0,0,6,15,108,219,211,148,6,22,139,179,77,7,113,63,9,189,218,209,178,17,68,171,89,66,203,196,27,135,73,87,177,31,202,180,227,130,29,40,33,146,141,155,98,125,34,236,211,194,13,207,79,146,159,73,219,173,112,47,150,56};
// Same fixture at 20 PCM units: nonzero decoded audio below the RMS floor.
inline constexpr uint8_t playout_quiet_fixture[]={72,11,242,138,130,154,1,145,55,165,197,210,118,40,85,212,234,177,172,43,7,106,230,15,67,194,235,32,185,172,40,82};
}
