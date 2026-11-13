#pragma once
#include <cstdint>
namespace dg_display {
enum class MappingState:uint8_t {Valid,Unknown,Skipped,Stale};
struct Presentation {MappingState state=MappingState::Unknown;int64_t speaker_us=0;uint32_t uncertainty_us=0,epoch=0;};
class PresentationClock {public:virtual ~PresentationClock()=default;virtual Presentation map(uint32_t timestamp,uint32_t clock_epoch)const noexcept=0;};
// Deterministic test clock. No production audio ledger is implemented in 9a.
class FakeClock:public PresentationClock {
public:
    enum class Kind:uint8_t {Audio,Plc,Insert,Drop,Trim};
    struct Span {uint32_t timestamp=0,length=0;int64_t speaker_us=0;Kind kind=Kind::Audio;};
    Span spans[16]{};unsigned count=0;uint32_t epoch=1,uncertainty_us=0;bool valid=true;
    Presentation map(uint32_t timestamp,uint32_t requested_epoch)const noexcept override;
    void restart()noexcept{++epoch;count=0;valid=false;}
};
}
