#pragma once
#include <cstdint>

namespace dg {
struct DemoTouchWindow {
    bool accept;
    uint32_t generation;
};
enum class DemoTouchDelivery { None, PhaseChanged, Forwarded };

// Poll may perform a bounded I2C read. Recheck the UI phase and generation
// afterwards so that a transition during that read cannot forward an old tap.
// App admission independently validates Ready and its own generation.
template<class Snapshot, class Poll, class Forward>
DemoTouchDelivery deliver_demo_touch(Snapshot snapshot, Poll poll, Forward forward) {
    const auto before=snapshot();
    if(!poll(before.accept))return DemoTouchDelivery::None;
    const auto after=snapshot();
    if(!before.accept || !after.accept || before.generation!=after.generation)
        return DemoTouchDelivery::PhaseChanged;
    forward();
    return DemoTouchDelivery::Forwarded;
}
}
