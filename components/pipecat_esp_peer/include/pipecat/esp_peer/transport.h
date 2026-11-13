#pragma once
#include <pipecat/transport.h>
#include <chrono>
#include <memory>
#include <functional>
#include "dg_render_tick.h"
#include "dg_media_token.h"
namespace pipecat {
struct EspPeerTransportOptions {
    unsigned sample_rate = 16000, channels = 1, opus_bitrate = 24000, opus_complexity = 0;
    bool opus_fec = false, opus_dtx = false;
    unsigned uplink_frame_ms = 20;
    // HIL fault injection: negotiate normally but discard the channel-open indication.
    bool diagnostic_channel_timeout = false;
    unsigned playout_target_frames = 7, playout_backlog_frames = 10;
    std::function<dg::RenderTick()> render_clock;
    size_t max_message_bytes = 16384;
    std::chrono::milliseconds connect_timeout{20000};
    unsigned agent_recv_timeout_ms = 10, loop_delay_ms = 1;
    unsigned jitter_cache_timeout_ms=100, owner_priority=6;
    bool dtls_aes_gcm=false, direct_socket_select=false;
    bool event_wake=false;
    bool receive_budget_after_read=true;
    bool eager_codec_initialization=false;
    bool full_call_timing=false, arrival_trace=false;
};
class EspPeerTransport final : public Transport {
public:
    explicit EspPeerTransport(EspPeerTransportOptions options = {});
    ~EspPeerTransport() override;
    void initialize(TransportObserver*) override;
    void set_start_bot_params(const APIRequest&) override;
    void connect(const nlohmann::json&) override;
    void disconnect() override;
    void send_ready_message(const rtvi::Message&) override;
    void send_message(const rtvi::Message&) override;
    size_t max_message_size() const override;
    int32_t send_user_audio(const int16_t*, size_t) override;
    int32_t read_bot_audio(int16_t*, size_t) override;
    int32_t read_bot_audio(int16_t*, size_t, dg::MediaToken&);
    bool cancel_bot_audio(int64_t now_us);
    void resume_bot_audio();
    bool media_ready() const;
    nlohmann::json stats() const;
    std::string stats_dump() const;
    // HIL: applies to one subsequent connect, without recreating workers.
    void fault_next_channel_open();
    // Freeze before capture is intentionally disabled; first freeze wins.
    void freeze_uplink();
    // Freezes permanently. Harness calls once, immediately before stopping.
    void dump_uplink_intervals();
    // App session worker only: render queued evidence outside the peer loop.
    void drain_wire_evidence();
    // Drain binary v1 arrival records (32 bytes each), outside media tasks.
    size_t drain_trace(void* records, size_t capacity);
    // Bench-only controls; accepted only while disconnected, bounded values.
    void diagnostic_option(const std::string& name,unsigned value);
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
