#pragma once
#include "rtp_ingress.h"
#include "dg_render_tick.h"
#include <array>
#include <mutex>
namespace pipecat::esp_peer {
// One owned payload store. Metadata locks never cover copies or decoding.
class Playout {
public:
    static constexpr unsigned capacity=32, frame_ticks=960, target_frames=7;
    struct Payload { uint8_t data[1500]; };
    using Storage=std::array<Payload,capacity>;
    enum class State { Empty, Prefill, Playing, Starved, Disconnected };
    enum class Kind { Decode, Plc, Zero, Insert, Wait, Idle, Disconnected, Prefill, Cancelled };
    struct Counters {
        uint64_t ingress=0, accepted=0, missing_slots=0, plc_calls=0, post_plc_zeros=0,
            decode_errors=0, starvation=0, drift_drop=0, drift_insert=0, latency_trim=0,
            discontinuity=0, ts_gap=0, decoded_packets=0, render_decisions=0,
            device_silence_blocks=0, media_advance=0, prefills=0, tick_resync=0,
            cancellations=0, cancelled_packets=0, cancelled_render_intervals=0;
        std::array<uint64_t,6> missing_run_ended{}; // 1,2,3,4,5,>=6; lifetime ended-run counts
        std::array<uint64_t,unsigned(IngressError::Count)> rejected{};
    };
    struct Snapshot {
        Counters counters;
        State state;
        uint64_t generation, render_tick;
        int64_t watermark, age_us;
        unsigned occupancy, ready_depth, ready_high_water, future_span;
    };
    struct Token { int slot=-1; uint64_t generation=0; bool candidate=false; explicit operator bool() const {return slot>=0;} };
    struct Decision {
        Kind kind=Kind::Wait;
        Token lease;
        uint8_t* payload=nullptr;
        size_t size=0;
        int64_t timestamp=0, render_us=0;
        uint64_t generation=0;
        uint32_t ssrc=0;
        unsigned missing_run=0, trimmed=0, skipped=0, reset_retired=0;
        // Decision-time telemetry: ready includes the frame about to be read;
        // drift filters the remaining ready-1 frames. Never a later snapshot.
        unsigned ready_depth=0, drift_filter_sum=0, drift_filter_count=0;
        int64_t drift_persistence_us=0;
        int64_t previous_watermark=0;
        uint8_t reset_reason=0;
        bool reset_decoder=false;
    };
    explicit Playout(Storage& storage);
    void authorize(uint32_t ssrc,uint32_t track_generation);
    Token reserve(const RtpAudioView& packet,int64_t ingress_us, const dg::DeviceProgress& device = {});
    bool publish(Token token,const uint8_t* payload,size_t size);
    bool insert(const RtpAudioView& packet,int64_t ingress_us, const dg::DeviceProgress& device = {});
    void reject(IngressError reason);
    Decision pull(const dg::RenderTick& tick,bool prior_pcm_valid);
    void finish(const Decision& decision,bool decoded);
    void release(Token token);
    // Metadata only; outstanding leases are invalidated, codec stays consumer-owned.
    bool cancel(int64_t now_us);
    void resume();
    void disconnect();
    Snapshot snapshot(int64_t now_us) const;
    bool ready() const;
    uint64_t generation() const;
private:
    enum class Ownership { Free, Writing, Ready, Reading };
    struct Slot {
        Ownership owner=Ownership::Free;
        uint64_t generation=0;
        int64_t ts=0, seq=0, ingress_us=0;
        uint32_t ssrc=0;
        size_t size=0;
        bool gap_before=false, cancelled=false;
    };
    void end_missing_run();
    void invalidate();
    void start(int64_t ts,int64_t seq,uint32_t ssrc,int64_t now,uint8_t reason=0);
    void drift_reset();
    int find(int64_t ts) const;
    unsigned contiguous(int64_t ts) const;
    unsigned useful() const;
    void retire(unsigned frames);
    unsigned drift(unsigned remaining,int64_t now,bool eligible,Decision& decision);
    Storage& storage_;
    mutable std::mutex mutex_;
    std::array<Slot,capacity> slots_{};
    State state_=State::Empty;
    Counters counts_{};
    uint64_t generation_=0, last_tick_=0, device_epoch_=0;
    bool cancel_latched_=false;
    int64_t mute_until_us_=0;
    bool tick_seen_=false, started_=false, reset_decoder_=true;
    uint32_t active_ssrc_=0, allowed_ssrc_=0, track_generation_=0,active_track_generation_=0;
    int64_t anchor_=0, startup_floor_=0, watermark_=0, high_ts_=0, high_seq_=0, prefill_us_=0, last_decode_us_=0;
    unsigned missing_run_=0, ready_high_water_=0, ingress_retired_=0;
    void retire_device(unsigned total);
    int candidate_=-1;
    unsigned reset_retired_=0;
    int64_t previous_watermark_=0;
    uint8_t reset_reason_=0;
    std::array<unsigned,50> occupancy_{};
    unsigned filter_at_=0, filter_count_=0, filter_sum_=0;
    int drift_sign_=0;
    int64_t persistence_us_=0, correction_us_=-1000000;
};
}
