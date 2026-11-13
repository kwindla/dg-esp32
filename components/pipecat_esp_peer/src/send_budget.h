#pragma once
#include <algorithm>
#include <cstdint>
namespace pipecat::esp_peer {
// One budget per owner iteration, shared by both sides of the receive pass.
struct SendBudget {
    unsigned sent=0;
    bool cutoff=false;
    bool available() const {return sent<4 && !cutoff;}
    void observe(int64_t duration_us,unsigned enomem_before,unsigned enomem_after) {
        ++sent;cutoff=duration_us>=20000 || enomem_after!=enomem_before;
    }
    static unsigned stale_snapshot(unsigned depth) {return std::min(depth,8u);}
};
}
