#pragma once
#include "cache.h"
#include "clock.h"
namespace dg_display {
struct Meta {uint32_t version=1,sequence=0;uint64_t epoch=0;char id[33]{};};
enum class Operation:uint8_t {Show,Hide,Fill};
struct SceneOperation {Operation kind=Operation::Hide;uint8_t layer=0;int16_t x=0,y=0;uint16_t color=0;char hash[65]{};};
enum class RequestKind:uint8_t {Scene,Load,Preload,Delete,Persist,Cancel};
enum class TimeKind:uint8_t {Immediate,Rtp,Arrival};
struct Request {
    Meta meta;RequestKind kind=RequestKind::Scene;SceneOperation operations[8]{};uint8_t count=0;
    TimeKind time=TimeKind::Immediate;uint32_t timestamp=0,clock_epoch=0;char utterance[33]{};
    char hash[65]{},url[513]{};uint32_t bytes=0;bool any_host=false;
};
enum class ResultState:uint8_t {Empty,Pending,Applied,Completed,Rejected,Cancelled,Expired,Superseded};
struct Result {Request request;ResultState state=ResultState::Empty;Error error=Error::None;uint64_t revision=0,order=0;bool persisted=false;};
enum class Admission:uint8_t {Accepted,Full,Invalid};
struct PostResult {Admission admission=Admission::Invalid;Error error=Error::Malformed;bool duplicate=false;};
struct Pending {Request request;Entry* reservations[8]{};bool occupied=false;unsigned result=0;};
struct RenderLease {Scene scene;Dirty dirty;uint64_t epoch=0;uint32_t generation=0;bool valid=false;};
// Serialized by the board's try-lock wrapper. Host tests call this deterministic core directly.
// No allocations, IO or waits occur in post()/tick()/snapshot(). All storage is caller-owned.
struct Engine {
    Cache& cache;PresentationClock& clock;Scene scene;Dirty dirty;Pending pending[16]{},admission_scratch;Result results[32]{};
    char tombstones[16][33]{};unsigned tombstone_next=0;uint64_t epoch=1;uint32_t last_sequence=0,generation=1;
    uint64_t requested=0,accepted=0,rendered=0,coalesced=0,pixels=0,deadline_misses=0,errors=0,dropped=0,superseded=0;
    uint64_t accepted_revision=0,rendering_revision=0,completed_revision=0,result_order=0;
    int64_t render_latency_us=30000,visual_lead_us=0;static constexpr int64_t Horizon=2000000,Late=100000;
    bool session=false,rendering=false;Scene rendering_scene;unsigned jobs[2]{},job_count=0;
    Engine(Cache& c,PresentationClock& p):cache(c),clock(p){}
    // Internal benchmark requests keep their own IDs and do not consume wire sequences.
    // Wire/console callers always use the default false.
    PostResult post(const Request& request,int64_t now,bool internal=false) noexcept;
    void tick(int64_t now) noexcept;
    bool snapshot(RenderLease& lease) noexcept;
    bool current(const RenderLease& lease)const noexcept{return lease.valid && lease.epoch==epoch && lease.generation==generation;}
    void complete(RenderLease& lease,bool success,uint64_t pushed_pixels) noexcept;
    Result* next_job() noexcept;
    void terminal(Result& result,Error error,bool persisted=false) noexcept;
    Result* result(const char* id) noexcept;
    void start_session() noexcept;
    void transport_down() noexcept;
    void end_session() noexcept;
    unsigned occupancy()const noexcept;
private:
    bool cancelled(const char* utterance)const noexcept;
    void release(Pending& item) noexcept;
    void reset_scene() noexcept;
    void cancel_pending(const char* utterance,uint8_t affected,bool all) noexcept;
    void apply(Pending& item) noexcept;
};
}
