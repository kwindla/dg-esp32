#pragma once
#include <atomic>
#include <cstdint>

namespace dg_audio {
// Fixed metadata only. One speaker-task writer, one capture-task writer,
// display reader. No locks, allocation, PCM copy, or console work in audio.
class CharacterSignals {
public:
    struct Sample { uint32_t speaker_power=0, mic_power=0; bool mic_open=false; };
    void speaker(uint32_t due_ms, uint32_t power) noexcept {
        auto& slot=slots_[next_++%32];
        auto version=slot.version.load(std::memory_order_relaxed);
        slot.version.store(version+1,std::memory_order_seq_cst);
        slot.due.store(due_ms,std::memory_order_seq_cst);
        slot.power.store(power,std::memory_order_seq_cst);
        slot.version.store(version+2,std::memory_order_seq_cst);
    }
    void capture(uint32_t now_ms,uint32_t power,bool open) noexcept {
        mic_power_.store(open?power:0,std::memory_order_relaxed);
        mic_stamp_.store(now_ms,std::memory_order_release);
    }
    Sample read(uint32_t now_ms) const noexcept {
        Sample result;int32_t youngest=31;
        for(const auto& slot:slots_) {
            const auto version=slot.version.load(std::memory_order_seq_cst);
            if(!version || (version&1))continue;
            const auto due=slot.due.load(std::memory_order_seq_cst);
            const auto power=slot.power.load(std::memory_order_seq_cst);
            if(slot.version.load(std::memory_order_seq_cst)!=version)continue;
            const auto age=int32_t(now_ms-due);
            if(age>=0 && age<youngest){youngest=age;result.speaker_power=power;}
        }
        if(uint32_t(now_ms-mic_stamp_.load(std::memory_order_acquire))<=200) {
            result.mic_power=mic_power_.load(std::memory_order_relaxed);
            result.mic_open=result.mic_power!=0;
        }
        return result;
    }
private:
    // Xtensa reports is_always_lock_free=false because of RMW operations;
    // this bridge uses only aligned32-bit loads/stores (no fetch/CAS).
    static_assert(sizeof(std::atomic<uint32_t>)==4 && alignof(std::atomic<uint32_t>)>=4);
    struct Slot {std::atomic<uint32_t> version{0},due{0},power{0};};
    Slot slots_[32];unsigned next_=0;
    std::atomic<uint32_t> mic_power_{0},mic_stamp_{0};
};
inline CharacterSignals character_signals;
}
