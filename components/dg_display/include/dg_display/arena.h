#pragma once
#include <cstddef>
#include <cstdint>
namespace dg_display {
// The arena the firmware allocates for itself, as the arguments of
// `disp arena bytes F W largest` (docs/display.md). F and the largest block
// are measured at the call. W is the retained measurement of the PSRAM a
// reconnect needs with no arena allocated; admission requires
// bytes <= F - W - 65536 - 524288.
constexpr unsigned SelfArenaBytes=491520;
constexpr unsigned ReconnectWorkingBytes=1287760;
class Arena {
    struct alignas(16) Block {size_t size=0;Block* next=nullptr;bool free=true;};
    uint8_t* memory_=nullptr;size_t budget_=0;Block* head_=nullptr;size_t allocations_=0;
public:
    bool init(void* memory,size_t bytes) noexcept;
    void* allocate(size_t bytes) noexcept;
    void release(void* pointer) noexcept;
    size_t charge(void* pointer) const noexcept;
    size_t used()const noexcept;
    size_t largest()const noexcept;
    size_t budget()const noexcept{return budget_;}
    size_t allocation_calls()const noexcept{return allocations_;}
};
}
