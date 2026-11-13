#pragma once
#include "timing_histogram.h"
#include "owner_totals.h"
#include "compact_timing.h"
#include "receive_budget_policy.h"
#include "optional_measures.h"
#include "pipecat/esp_peer/stat_json.h"
#include <nlohmann/json.hpp>
#include <cstdint>
#include <memory>
#include <mutex>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
namespace pipecat::esp_peer {
// Inclusive cycle counts: task preemption and blocking remain in the interval.
// Compare with task runtime deltas, never treat these as exclusive CPU costs.
enum class PeerPart { Main, SendAudio, SendData, Control, Wait, Recv, Send,
    Decrypt, Encrypt, DtlsRead, DtlsWrite, Select, LwipSelect, Iteration, DataCallback, HeapSample, Count };
struct PeerMeasure {
    OwnerTotals totals;
    CompactTiming histogram;
    void add(uint32_t cycles_delta, unsigned us) {
        totals.add(cycles_delta,us);histogram.add(us);
    }
};
// Binary v1 records, little endian, 32 bytes. RTP is observed at socket read
// and after authentication, before the opaque native jitter cache.
struct ArrivalRecord {
    int64_t time_us;
    uint32_t timestamp, ssrc, value, aux;
    uint16_t sequence, bytes;
    uint8_t kind, flags;
    uint16_t reserved;
};
static_assert(sizeof(ArrivalRecord)==32);
class ArrivalRing {
public:
    ArrivalRing()=default;
    void enable();
    ~ArrivalRing();
    void record(ArrivalRecord row);
    size_t drain(ArrivalRecord* rows,size_t capacity);
    StatJson stats() const;
private:
    static constexpr size_t capacity=8192;
    ArrivalRecord* rows_=nullptr;
    mutable portMUX_TYPE mux_=portMUX_INITIALIZER_UNLOCKED;
    uint64_t head_=0,tail_=0,overwritten_=0;
};
class PeerTrace {
public:
    enum Kind { Wire=1, Authenticated=2, Callback=3, Plc=4, Rebuffer=5,
                Drift=6, Drop=7, SlowLoop=8, Wifi=9, RtpGap=10, SlowCall=11, UplinkWire=12, Ingress=13, Render=14, Trim=15, Reset=16, DeviceEpoch=17, Terminal=18, DtlsApplication=19 };
    explicit PeerTrace(std::shared_ptr<ArrivalRing> ring,uint32_t generation,bool detailed);
    OptionalMeasures<PeerMeasure,unsigned(PeerPart::Count),unsigned(PeerPart::Iteration)> measures;
    std::atomic<uint32_t> iterations{0}, empty_iterations{0};
    std::atomic<uint32_t> direct_select_calls{0};
    std::atomic<uint32_t> dtls_app_datagrams{0};
    std::atomic<uint32_t> socket_send_errors{0}, socket_send_enomem{0}, socket_send_eagain{0};
    std::atomic<int> socket_send_last_errno{0};
    std::atomic<uint32_t> event_wait_calls{0}, event_notifications{0};
    bool direct_socket_select=false;
    const bool full_call_timing;
    bool arrival_trace=false;
    ReceiveBudget receive_budget;
    int64_t connect_deadline_us=0;
    const std::atomic<bool>* stopping=nullptr;
    bool handshake_failed=false;
    std::atomic<uint32_t> receive_budget_yields{0};
    std::atomic<uint32_t> prechat_receive_budget_activations{0};
    bool event_wake=false, selecting_read=false;
    // Producers borrow the active select semaphore only under this lock.
    // It is disarmed before lwIP can destroy its select callback.
    void notify_owner();
    void arm_wait(void* semaphore);
    void disarm_wait();
    void acknowledge_work();
    TaskHandle_t owner=nullptr;
    void record(ArrivalRecord row);
    size_t drain(ArrivalRecord* rows, size_t capacity);
    StatJson stats() const;
    void rtp(unsigned kind, const void* data, int bytes);
    void mark(unsigned kind, uint32_t value=0, uint32_t aux=0);
private:
    uint32_t rtp_ssrc_=0;uint16_t rtp_highest_=0;bool rtp_seen_=false;
    std::mutex wake_mutex_;
    void* wait_sem_=nullptr;
    bool work_pending_=false;
    std::shared_ptr<ArrivalRing> ring_;
    uint32_t generation_;
};
static_assert(sizeof(PeerTrace)<1024,"Dormant detailed counters must not consume resident internal heap");
std::shared_ptr<PeerTrace> make_peer_trace(std::shared_ptr<ArrivalRing> ring,uint32_t generation,bool detailed=false);
// One peer owner exists at a time. Only that task accesses the active pointer;
// it is set before native calls and cleared before owner teardown.
extern thread_local PeerTrace* active_peer_trace;
class PeerTimer {
public:
    explicit PeerTimer(PeerPart part);
    ~PeerTimer();
private:
    PeerTrace* trace_;
    PeerPart part_;
    uint32_t cycles_;
    int64_t time_;
};
}
