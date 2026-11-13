#include "message_admission.h"
#include "pipecat/esp_peer/transport.h"
#include "pipecat/esp/http.h"
#include "pipecat/errors.h"
#include "params.h"
#include "framer.h"
#include "playout.h"
#include "interrupt_sequence.h"
#include "playout_pcm.h"
#include "msg_queue.h"
#include "inbound_queue.h"
#include "send_budget.h"
#include "opus.h"
#include "wire_contract.h"
#include "uplink_intervals.h"
#include "dg_console.h"
#include <cstdio>
#include "uplink_frame.h"
#include "dg_net.h"
#include "dg_diagnostics.h"
#include "timing_histogram.h"
#include "persistent_worker.h"
#include "peer_trace.h"
#include "handshake_policy.h"
#include "esp_peer.h"
#include "esp_peer_default.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "pthread_config.h"
#include "esp_log.h"
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "lwip/netdb.h"
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>
extern "C" unsigned pipecat_empty_udp_count(void);
extern "C" unsigned dg_audio_i2s_underflows(void) __attribute__((weak));
extern "C" unsigned dg_audio_mic_in_turn_clips(void) __attribute__((weak));
extern "C" unsigned dg_audio_mic_gate_open_clips(void) __attribute__((weak));
extern "C" unsigned dg_audio_mic_gate_closed_clips(void) __attribute__((weak));
extern "C" int dg_audio_output_volume_db(void) __attribute__((weak));
extern "C" unsigned dg_audio_i2s_write_errors(void) __attribute__((weak));
extern "C" unsigned dg_audio_i2s_write_timeouts(void) __attribute__((weak));
extern "C" void dg_audio_media_invalidated(void) __attribute__((weak));
extern "C" void dg_heap_window_sample(bool) __attribute__((weak));
namespace pipecat {
#if CONFIG_DG_PEER_RX_STACK_PSRAM
static constexpr unsigned rx_stack_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
#else
static constexpr unsigned rx_stack_caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
#endif
using namespace esp_peer;
namespace {
constexpr const char* TAG = "esp-peer";
int64_t now_ms() { return esp_timer_get_time() / 1000; }
void sleep_ms(unsigned ms) { vTaskDelay(std::max(TickType_t(1), pdMS_TO_TICKS(ms))); }
void* psalloc(size_t bytes) {
    void* p = heap_caps_calloc(1, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!p) throw std::bad_alloc();
    return p;
}
template<class T> struct PsBuffer {
    T* data;
    size_t count;
    explicit PsBuffer(size_t n = 1) : data(static_cast<T*>(psalloc(n * sizeof(T)))), count(n) {
        std::uninitialized_value_construct_n(data, count);
    }
    ~PsBuffer() { std::destroy_n(data, count); free(data); }
    PsBuffer(const PsBuffer&) = delete;
    PsBuffer& operator=(const PsBuffer&) = delete;
    T& operator*() { return *data; }
};
template<class T> struct InternalObject {
    T* data;
    template<class... A> explicit InternalObject(A&&... args) {
        void* memory=heap_caps_malloc(sizeof(T),MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
        if(!memory)throw std::bad_alloc();
        try {data=new(memory)T(std::forward<A>(args)...);}catch(...) {free(memory);throw;}
    }
    ~InternalObject() {data->~T();free(data);}
    T* operator->() const {return data;}
    InternalObject(const InternalObject&)=delete;
};
// IDF copies each queue element into caller-owned PSRAM. Control block internal.
template<class T> struct Queue {
    PsBuffer<uint8_t> bytes;
    StaticQueue_t control{};
    QueueHandle_t handle;
    explicit Queue(size_t n) : bytes(n * sizeof(T)),
        handle(xQueueCreateStatic(n, sizeof(T), bytes.data, &control)) {
        if (!handle) throw std::bad_alloc();
    }
    ~Queue() { vQueueDelete(handle); }
    bool send(const T& item) { return xQueueSend(handle, &item, 0) == pdTRUE; }
    bool recv(T& item, unsigned ms = 0) { return xQueueReceive(handle, &item, pdMS_TO_TICKS(ms)) == pdTRUE; }
    unsigned depth() const { return uxQueueMessagesWaiting(handle); }
};
struct Critical {
    portMUX_TYPE& mux;
    explicit Critical(portMUX_TYPE& m) : mux(m) { portENTER_CRITICAL(&mux); }
    ~Critical() { portEXIT_CRITICAL(&mux); }
};
struct Packet { uint32_t pts; int size; int64_t time_us; uint8_t data[1500]; };
// Message records are rendered by send_message's caller. The peer owner only
// copies fixed buffers after successful native sends; app-session prints them.
struct WireEvidence { size_t size=0; bool local_ordered=false; char data[1024]{}; };
struct Text { size_t size; char data[32769]; bool closing_control=false; WireMessage evidence=WireMessage::None; WireEvidence record; };
struct Audio { int count; int16_t pcm[320]; };
void phase(const char* name) {
    dg_heap_phase(name, true);
}
void evidence_line(const std::string& line) {
    flockfile(stdout);dg_console::write(line.data(),line.size());funlockfile(stdout);
}
void wire_emit(const std::string& key,const nlohmann::json& value) noexcept {
    try {evidence_line("WIRE_CONTRACT "+key+"="+value.dump()+"\n");}
    catch(...) {dg_deferred_log("WIRE_CONTRACT ERROR");}
}
__attribute__((noinline))
void emit_encoder_settings(const EncoderSettings& enc) {
    // Keep startup JSON temporaries out of the frame retained while encoding.
    wire_emit("opus_enc",{{"sample_rate",enc.sample_rate},{"channels",enc.channels},{"frame_ms",enc.frame_ms},
        {"bitrate",enc.bitrate},{"complexity",enc.complexity},{"application",enc.application},
        {"bits_per_sample",enc.bits_per_sample},{"fec",enc.fec},{"dtx",enc.dtx},{"vbr",enc.vbr}});
}
void intervals_emit(const UplinkSnapshot& v) noexcept {
    try {
        std::string line="UPLINK_INTERVALS gen="+std::to_string(v.generation)+" start_us="+std::to_string(v.start_us)+
            " end_us="+std::to_string(v.end_us)+" n="+std::to_string(v.n)+" attempts="+std::to_string(v.attempts)+
            " ok="+std::to_string(v.ok)+" max_us="+std::to_string(v.max_us)+
            " last_ok_age_us="+std::to_string(v.last_ok_age_us)+" hist=[";
        for(size_t i=0;i<v.hist.size();++i){if(i)line+=',';line+=std::to_string(v.hist[i]);}
        evidence_line(line+"]\n");
    }catch(...) {dg_deferred_log("UPLINK_INTERVALS ERROR");}
}
uint32_t signaling_ip(const std::string& url) {
    auto begin = url.find("://");
    if (begin == std::string::npos) throw TransportStartError("Invalid signaling URL");
    begin += 3;
    auto end = url.find_first_of(":/?#", begin);
    auto host = url.substr(begin, end - begin);
    addrinfo hint{}, *result = nullptr;
    hint.ai_family = AF_INET; hint.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host.c_str(), nullptr, &hint, &result) != 0 || !result)
        throw TransportStartError("Signaling host lookup failed");
    uint32_t ip = ntohl(reinterpret_cast<sockaddr_in*>(result->ai_addr)->sin_addr.s_addr);
    freeaddrinfo(result); return ip;
}
}
struct EspPeerTransport::Impl {
    struct Session : dg::MediaSource {
        EspPeerTransportOptions options;
        TransportObserver* observer;
        uint32_t generation;
        PsBuffer<UplinkIntervals> uplink;
        portMUX_TYPE uplink_mux=portMUX_INITIALIZER_UNLOCKED;
        std::atomic<unsigned> wire_seen{0};
        bool dc_evidence=false,channel_ordered=false;
        Queue<WireEvidence> wire_evidence{5}; // Four first-message kinds + channel-open.
        PsBuffer<WireEvidence> callback_evidence;
        void freeze_uplink(int64_t now) {Critical lock(uplink_mux);uplink.data->freeze(now);}
        bool claim_uplink(UplinkSnapshot& out) {
            Critical lock(uplink_mux);return uplink.data->claim(esp_timer_get_time(),out);
        }
        std::atomic<bool> stop{false}, finish_owner{false}, opened{false}, remote{false}, failed{false};
        std::atomic<bool> connected{false};
        TaskHandle_t owner_task = nullptr;
        esp_peer_handle_t peer = nullptr;
        // Retain all native configuration pointers until close, even if a
        // future binary version retains rather than copies these strings.
        ConnectionParams connection;
        std::vector<esp_peer_ice_server_cfg_t> servers;
        esp_peer_default_cfg_t extra{};
        esp_peer_cfg_t config{};
        esp_peer_rtp_transform_cb_t receiver_transform{};
        std::thread owner, tx, rx;
        bool owner_started=false;
        std::atomic<bool> jobs_exited{false};
        std::unique_ptr<OpusEncoder> encoder;
        std::unique_ptr<OpusDecoder> codec;
        Queue<Text> offer{1}, answer{1}, outbound{4};
        // Eight encoded packets (160 ms at 20 ms, 320 ms at 40 ms) cover a
        // fragmented SCTP send on the sole owner without dropping capture
        // frames; PCM remains four-deep.
        Queue<Packet> encoded{8};
        Queue<int> events{32};
        PsBuffer<char> message_storage;
        MessageQueue messages;
        PsBuffer<char> inbound_storage;
        InboundQueue inbound;
        PsBuffer<std::array<PcmFrame, 4>> pcm_storage;
        Framer framer;
        PsBuffer<Text> callback_text, owner_text;
        PsBuffer<Packet> owner_audio;
        portMUX_TYPE msg_mux = portMUX_INITIALIZER_UNLOCKED;
        portMUX_TYPE pcm_mux = portMUX_INITIALIZER_UNLOCKED;
        portMUX_TYPE stats_mux = portMUX_INITIALIZER_UNLOCKED;
        std::mutex read_mutex, messages_mutex;
        Audio remainder{};
        PsBuffer<Playout::Storage> playout_storage;
        PsBuffer<std::array<std::atomic<unsigned>, 1501>> payload_sizes;
        InternalObject<Playout> playout{*playout_storage};
        AudioTrack audio_track;
        uint32_t track_generation=0;
        PlayoutPcm pcm_renderer;
        uint64_t pcm_generation=0;
        dg::RenderTick last_render{};
        uint64_t trace_device_epoch=0;
        bool valid(uint64_t media_generation) const override {
            return !stop && !remote && !failed && playout->generation()==media_generation;
        }
        std::shared_ptr<PeerTrace> trace;
        uint64_t pulled_frames=0;
        int64_t first_pull_us=0, last_pull_us=0;
        size_t offset = 0;
        uint16_t stream = 0;
        std::atomic<int64_t> open_us{0},offer_us{0},answer_us{0},connected_us{0},channel_us{0};
        std::atomic<unsigned> sent{0}, packets{0}, dropped{0}, decode_errors{0}, send_errors{0};
        enum DropReason { InvalidData, MessageQueueFull,
            EncodedQueue, MessageParse, EventQueue, InvalidSdp,
            OfferQueue, WorkerException, OutboundQueue, UplinkGap, SendCutoff, AdmissionBudget, AdmissionShape, AdmissionHeap, AdmissionOverflow, DropReasonCount };
        std::atomic<unsigned> drop_reasons[DropReasonCount]{};
        // Legacy rejection counter remains in the schema. Valid signaled RTP
        // is now admitted before chat opens, rather than counted as a loss.
        std::atomic<unsigned> preconnect_packets{0};
        std::atomic<unsigned> rtp_before_chat_packets{0};
        std::atomic<int64_t> first_rtp_before_chat_us{0};
        MessageWorkBudget message_work;
        InterruptSequence interrupt_sequence;
        std::atomic<unsigned> interrupt_duplicates{0},cancel_serial{0};
        unsigned consumed_cancel_serial=0;
        std::atomic<uint64_t> cancelled_pcm_samples{0};
        std::atomic<unsigned> data_received{0},data_dispatched{0},sdk_backpressure{0};
        // Messages carried over past their arrival pass's work budget, and the
        // carried-over depth: current and session high water. Owner-written.
        std::atomic<unsigned> data_deferred{0},deferred_depth{0},deferred_max{0};
        std::atomic<bool> closing_flushed{false},closing_flush_timeout{false};
        std::atomic<unsigned> closing_control_sent{0}, send_budget_cutoffs{0}, stale_audio_skipped{0};
        std::atomic<unsigned> bytes_sent{0}, bytes_received{0};
        std::atomic<uint32_t> last_audio_ms{0};
        std::atomic<unsigned> loop_max_us{0}, latency_max_us{0}, latency_total_us{0}, latency_count{0};
        std::atomic<unsigned> encode_us{0}, decode_us{0};

        PsBuffer<TimingHistogram> encode_hist, decode_hist, silence_decode_hist, speech_decode_hist;
        std::atomic<unsigned> tx_stack{0}, decode_stack{0}, rx_stack{0}, owner_stack{0};
        std::atomic<unsigned> server_received{0}, server_lost{0}, server_max_gap_ms{0};
        std::atomic<bool> server_reported{false};

        std::string pc_id;
        explicit Session(EspPeerTransportOptions o, TransportObserver* obs, uint32_t gen,std::shared_ptr<ArrivalRing> ring)
            : options(o), observer(obs), generation(gen),
              message_storage(MessageQueue::capacity * MessageAdmission::slot_bytes(o.max_message_bytes)),
              messages(message_storage.data, MessageQueue::capacity * MessageAdmission::slot_bytes(o.max_message_bytes), MessageAdmission::slot_bytes(o.max_message_bytes)),
              inbound_storage(InboundQueue::capacity * MessageAdmission::slot_bytes(o.max_message_bytes)),
              inbound(inbound_storage.data, InboundQueue::capacity * MessageAdmission::slot_bytes(o.max_message_bytes), MessageAdmission::slot_bytes(o.max_message_bytes)),
              framer(*pcm_storage),trace(make_peer_trace(std::move(ring),gen,options.full_call_timing)) {
            *uplink.data=UplinkIntervals(gen);
            if(options.eager_codec_initialization) {
                // Allocate in a fixed order before publishing the session or
                // launching workers; each codec is then processed by one worker.
                codec=std::make_unique<OpusDecoder>();
                encoder=std::make_unique<OpusEncoder>(options.opus_bitrate,options.opus_complexity,
                    options.opus_fec,options.opus_dtx,options.uplink_frame_ms);
            }
            trace->event_wake=options.event_wake;
            trace->arrival_trace=options.arrival_trace;
            if(options.event_wake) {
                options.direct_socket_select=true;options.agent_recv_timeout_ms=20;options.loop_delay_ms=0;
            }
        }
        void assert_owner() const { assert(xTaskGetCurrentTaskHandle() == owner_task); }
        void retire_media() {
            freeze_uplink(esp_timer_get_time());remote=true;playout->disconnect();
            trace->notify_owner();
            if(dg_audio_media_invalidated)dg_audio_media_invalidated();
        }
        void fault() { if(!failed)ESP_LOGW(TAG,"PEER_FAULT caller=%p",__builtin_return_address(0)); failed=true;retire_media(); }
        void drop(DropReason reason,uint32_t detail=0,uint32_t stream=0) {
            ++dropped;++drop_reasons[reason];
            // flags=1 distinguishes queue/validation drops from playout drops.
            trace->record({esp_timer_get_time(),stream,0,uint32_t(reason),detail,0,0,PeerTrace::Drop,1,0});
        }
        void event(int e) { if (!events.send(e)) { drop(EventQueue,e); fault(); } }
        static int state(esp_peer_state_t value, void* context) {
            auto& s = *static_cast<Session*>(context); s.assert_owner();
            if (!s.stop) {
                if(value==ESP_PEER_STATE_DISCONNECTED || value==ESP_PEER_STATE_CONNECT_FAILED ||
                    value==ESP_PEER_STATE_DATA_CHANNEL_CLOSED || value==ESP_PEER_STATE_DATA_CHANNEL_DISCONNECTED) {
                    s.trace->mark(PeerTrace::Terminal,unsigned(value),0);
                    ESP_LOGW(TAG,"PEER_STATE terminal=%d",int(value));
                    s.retire_media();
                }
                s.event(int(value));
            }
            return 0;
        }
        static int channel_open(esp_peer_data_channel_info_t* info, void* context) {
            auto& s = *static_cast<Session*>(context); s.assert_owner();
            if (!s.stop && !s.options.diagnostic_channel_timeout && info->label && std::strcmp(info->label, "chat") == 0) {
                // Owner-only plain 64-bit deadline; stop remains live afterward.
                s.trace->connect_deadline_us=0;
                s.stream = info->stream_id; s.channel_us=esp_timer_get_time();s.opened = true;
                if(!s.dc_evidence) {
                    s.dc_evidence=true;
                    // The accepted label is observed; ordering is local config,
                    // and the SDK has no protocol argument/getter. No JSON/IO here.
                    auto& record=*s.callback_evidence;
                    record.size=0;record.local_ordered=s.channel_ordered;
                    std::memcpy(record.data,info->label,5);
                    if(!s.wire_evidence.send(record))dg_deferred_log("WIRE_CONTRACT ERROR queue-full");
                }
            }
            return 0;
        }
        static int channel_close(esp_peer_data_channel_info_t*, void* context) {
            auto& s = *static_cast<Session*>(context); s.assert_owner();
            if (!s.stop) {s.trace->mark(PeerTrace::Terminal,ESP_PEER_STATE_DATA_CHANNEL_CLOSED,1);s.retire_media();}
            return 0;
        }
        static int local_sdp(esp_peer_msg_t* msg, void* context) {
            auto& s = *static_cast<Session*>(context); s.assert_owner();
            if (s.stop || msg->type != ESP_PEER_MSG_TYPE_SDP) return 0;
            if (!msg->data || msg->size <= 0 || msg->size > 32768) { s.drop(InvalidSdp,msg->size); s.fault(); return 0; }
            auto& text = *s.callback_text;
            text.size = msg->size;
            if (msg->data[text.size - 1] == 0) --text.size;
            std::memcpy(text.data, msg->data, text.size); text.data[text.size] = 0;
            if (!s.offer.send(text)) { s.drop(OfferQueue); s.fault(); }
            return 0;
        }
        static int identity_size(esp_peer_rtp_frame_t* frame, bool* in_place, void*) {
            *in_place=true; frame->encoded_size=frame->orig_size; return 0;
        }
        static int identity_receive(esp_peer_rtp_frame_t* frame, void* context) {
            auto& s=*static_cast<Session*>(context); s.assert_owner();
            s.trace->rtp(PeerTrace::Ingress,frame->orig_data,frame->orig_size);
            RtpAudioView packet;
            const auto admission=admit_audio_rtp(frame->orig_data,frame->orig_size,s.audio_track,
                                      s.stop || s.remote || s.failed,s.opened,packet);
            const auto reason=admission.error;
            if(reason!=IngressError::None) {
                s.playout->reject(reason);
                // flags=2: ingress validation, with its actual reason and the
                // packet identity when parsing reached it. Optional ring only.
                s.trace->record({esp_timer_get_time(),packet.timestamp,packet.ssrc,
                    unsigned(reason),unsigned(s.opened.load()),packet.sequence,
                    uint16_t(packet.size),PeerTrace::Drop,2,0});
            }
            else {
                // RTP can precede the SCTP chat callback. Apply B7's native
                // receive bound immediately, including this pass's preceding
                // setup reads, before reservation/copy/accounting work.
                if(s.trace->receive_budget.admitted_media() && admission.before_chat)
                    ++s.trace->prechat_receive_budget_activations;
                const auto now=esp_timer_get_time();
                if(admission.before_chat) {
                    if(s.rtp_before_chat_packets.fetch_add(1)==0) {
                        s.first_rtp_before_chat_us=now;
                        dg_deferred_log("RTP_BEFORE_CHAT t=%lld gen=%u seq=%u ssrc=%u admitted=1",
                            (long long)now,unsigned(s.generation),unsigned(packet.sequence),unsigned(packet.ssrc));
                    }
                }
                ++s.packets;s.bytes_received+=packet.size;++(*s.payload_sizes)[packet.size];
                s.last_audio_ms=uint32_t(now/1000);
                s.playout->insert(packet,now,dg::device_clock.snapshot());
            }
            frame->encoded_size=frame->orig_size;
            return 0;
        }
        static int audio_info(esp_peer_audio_stream_info_t* info, void* context) {
            auto& s = *static_cast<Session*>(context); s.assert_owner();
            if (info->codec != ESP_PEER_AUDIO_CODEC_OPUS) s.fault();
            return 0;
        }
        static int audio_data(esp_peer_audio_frame_t* frame, void* context) {
            auto& s = *static_cast<Session*>(context); s.assert_owner();
            // The ordinary native callback is a diagnostic shadow only.
            if(!s.stop && frame->data && frame->size>0)
                s.trace->mark(PeerTrace::Callback,frame->pts,frame->size);
            return 0;
        }
        static int data(esp_peer_data_frame_t* frame, void* context) {
            auto& s = *static_cast<Session*>(context); s.assert_owner();
            PeerTimer timer(PeerPart::DataCallback);
            if (s.stop) return 0;
            ++s.data_received;
            if (!frame->data || frame->size <= 0 || size_t(frame->size) > MessageAdmission::slot_bytes(s.options.max_message_bytes) ||
                frame->type != ESP_PEER_DATA_CHANNEL_STRING) {
                s.drop(InvalidData,(uint32_t(frame->type)<<24)|(uint32_t(frame->size)&0xffffff),frame->stream_id);return 0;
            }
            const auto wire=std::string_view(reinterpret_cast<const char*>(frame->data),size_t(frame->size));
            InboundQueue::Lease lease;
            {Critical lock(s.msg_mux);lease=s.inbound.reserve(wire.size(),now_ms());}
            if(!lease) {
                // Declared capacity (64 owned slots) exhausted: visible loss.
                // No classification/allocation on the overloaded native owner.
                s.drop(AdmissionOverflow,(uint32_t(frame->type)<<24)|(uint32_t(frame->size)&0xffffff),frame->stream_id);return 0;
            }
            // The native buffer is not retained: copy into the owned slot, then
            // publish READY within this pass's work budget, else HELD for a
            // later pass. Once work is held, later arrivals queue behind it.
            std::memcpy(lease.data,wire.data(),wire.size());
            const bool defer=s.inbound.held() || !s.message_work.admit(wire.size(),uint64_t(esp_timer_get_time()));
            unsigned held;
            {Critical lock(s.msg_mux);s.inbound.publish(lease,defer);held=s.inbound.held();}
            if(defer) {
                ++s.data_deferred;s.deferred_depth=held;
                s.deferred_max=std::max(s.deferred_max.load(),held);
            }
            return 0;
        }
        void send_audio_phase(SendBudget& budget,unsigned phase_limit) {
            for(unsigned i=0;i<phase_limit && opened && !stop && !remote && !failed &&
                budget.available() && encoded.recv(*owner_audio);++i) {
                auto& packet=*owner_audio;
                unsigned latency=esp_timer_get_time()-packet.time_us;
                latency_max_us=std::max(latency_max_us.load(),latency);
                latency_total_us+=latency;++latency_count;
                esp_peer_audio_frame_t frame{};
                frame.pts=packet.pts;frame.data=packet.data;frame.size=packet.size;
                auto enomem_before=trace->socket_send_enomem.load();
                auto begin=esp_timer_get_time();
                int error;int64_t submitted;
                {PeerTimer timer(PeerPart::SendAudio);
                 error=esp_peer_send_audio(peer,&frame);submitted=esp_timer_get_time();
                 if(error)++send_errors;
                 else {++sent;bytes_sent+=packet.size;}}
                {Critical lock(uplink_mux);uplink.data->submit(submitted,error==0);}
                budget.observe(esp_timer_get_time()-begin,enomem_before,trace->socket_send_enomem.load());
                if(budget.cutoff) {
                    ++send_budget_cutoffs;
                    // Snapshot first. FIFO means later producer arrivals stay
                    // behind this bounded cohort, even while slots are freed.
                    auto stale=SendBudget::stale_snapshot(encoded.depth());
                    for(unsigned n=0;n<stale && encoded.recv(*owner_audio);++n) {
                        ++stale_audio_skipped;drop(SendCutoff,owner_audio.data->pts);
                    }
                }
            }
        }
        void owner_run() {
            owner_task = xTaskGetCurrentTaskHandle();
            trace->owner=owner_task;active_peer_trace=trace.get();
            trace->direct_socket_select=options.direct_socket_select;
            try {
                assert_owner();
                receiver_transform={identity_size,identity_receive};
                if (esp_peer_set_rtp_transformer(peer, ESP_PEER_RTP_TRANSFORM_ROLE_RECEIVER,
                    &receiver_transform, this)) throw TransportStartError("Receiver transformer registration failed");
                dg_deferred_log("RTP_INGRESS identity=1 qualification=0 schema=2 cache_ms=%u cache_bytes=16384",options.jitter_cache_timeout_ms);
                if (esp_peer_new_connection(peer)) fault();
                while (!finish_owner) {
                    PeerTimer iteration_timer(PeerPart::Iteration);
                    message_work.begin();
                    if(inbound.held()) {
                        // Carried-over work is admitted first, FIFO, within this
                        // pass's budget; new arrivals stay behind it.
                        const auto now=uint64_t(esp_timer_get_time());
                        Critical lock(msg_mux);
                        inbound.promote([&](size_t bytes){return message_work.admit(bytes,now);});
                        deferred_depth=inbound.held();
                    }
                    if(dg_heap_window_sample) {PeerTimer sample_timer(PeerPart::HeapSample);dg_heap_window_sample(opened && !stop && !remote && !failed);}
                    if (!stop && !failed && !remote) {
                        SendBudget send_budget;
                        send_audio_phase(send_budget,2);
                        auto begin = esp_timer_get_time();
                        auto before=packets.load();
                        trace->receive_budget.begin(opened,options.receive_budget_after_read,begin);
                        {PeerTimer timer(PeerPart::Main);if (esp_peer_main_loop(peer)) fault();}
                        if(trace->handshake_failed)fault();
                        trace->receive_budget.finish();
                        unsigned duration=esp_timer_get_time()-begin;

                        loop_max_us = std::max(loop_max_us.load(),duration);
                        ++trace->iterations;if(before==packets.load()) ++trace->empty_iterations;
                        trace->acknowledge_work();
                        {PeerTimer control_timer(PeerPart::Control);
                        int e;
                        while (events.recv(e)) {
                            if (e == ESP_PEER_STATE_CONNECTED) { connected_us=esp_timer_get_time();connected = true; phase("connected"); }
                            if (e == ESP_PEER_STATE_DATA_CHANNEL_CONNECTED) {
                                esp_peer_data_channel_cfg_t channel{};
                                channel.type = ESP_PEER_DATA_CHANNEL_RELIABLE;
                                channel.ordered = true; channel.label = const_cast<char*>("chat");
                                channel_ordered=channel.ordered;
                                if (esp_peer_create_data_channel(peer, &channel)) fault();
                            }
                            if (e == ESP_PEER_STATE_DISCONNECTED || e == ESP_PEER_STATE_CONNECT_FAILED ||
                                e == ESP_PEER_STATE_DATA_CHANNEL_CLOSED || e == ESP_PEER_STATE_DATA_CHANNEL_DISCONNECTED)
                                retire_media();
                        }
                        if (answer.recv(*owner_text)) {
                            esp_peer::audio_track({owner_text.data->data,owner_text.data->size},++track_generation,audio_track);
                            if(!audio_track.generation)throw TransportStartError("No signaled remote Opus audio source");
                            playout->authorize(audio_track.ssrc,audio_track.generation);
                            esp_peer_msg_t msg{}; msg.type = ESP_PEER_MSG_TYPE_SDP;
                            msg.data = reinterpret_cast<uint8_t*>(owner_text.data->data); msg.size = owner_text.data->size;
                            if (esp_peer_send_msg(peer, &msg)) fault();
                            answer_us=esp_timer_get_time();
                            phase("answer-applied");
                        }
                        if (opened) {
                            send_audio_phase(send_budget,4-send_budget.sent);
                            if (outbound.recv(*owner_text)) {
                                esp_peer_data_frame_t frame{};
                                frame.type = ESP_PEER_DATA_CHANNEL_STRING; frame.stream_id = stream;
                                frame.data = reinterpret_cast<uint8_t*>(owner_text.data->data);
                                frame.size = owner_text.data->size;
                                PeerTimer timer(PeerPart::SendData);
                                if (esp_peer_send_data(peer, &frame)) ++send_errors;
                                else {
                                    if(owner_text.data->closing_control)++closing_control_sent;
                                    const auto bit=unsigned(owner_text.data->evidence);
                                    if(bit && !(wire_seen.load(std::memory_order_relaxed)&bit)) {
                                        wire_seen.fetch_or(bit,std::memory_order_relaxed);
                                        if(owner_text.data->record.size && !wire_evidence.send(owner_text.data->record))
                                            dg_deferred_log("WIRE_CONTRACT ERROR queue-full");
                                    }
                                }
                            }
                        }
                        if(options.event_wake && (encoded.depth() || outbound.depth() || answer.depth() || inbound.held()))trace->notify_owner();
                        }
                    } else if(!closing_flushed) {
                        // Producers reject new work after stop. Only the queued
                        // SDK closing control survives; ordinary requests are
                        // cancelled with the session. All native calls stay on
                        // this owner, before disconnect/close.
                        while(outbound.recv(*owner_text)) {
                            if(!opened || !owner_text.data->closing_control)continue;
                            esp_peer_data_frame_t frame{};
                            frame.type=ESP_PEER_DATA_CHANNEL_STRING;frame.stream_id=stream;
                            frame.data=reinterpret_cast<uint8_t*>(owner_text.data->data);
                            frame.size=owner_text.data->size;
                            PeerTimer timer(PeerPart::SendData);
                            if(esp_peer_send_data(peer,&frame))++send_errors;
                            else ++closing_control_sent;
                        }
                        if(closing_control_sent) {
                            PeerTimer timer(PeerPart::Main);
                            esp_peer_main_loop(peer);
                        }
                        closing_flushed=true;
                    }
                    // Always block, including after CPU-bound native work.
                    if(options.loop_delay_ms) {PeerTimer timer(PeerPart::Wait);sleep_ms(options.loop_delay_ms);}
                    else vTaskDelay(1); // A busy pass is not evidence of a blocking wait.
                }
            } catch (...) { fault(); }
            // Producers and consumers are joined by disconnect before this flag.
            while (!finish_owner) sleep_ms(1);
            assert_owner();
            if (peer) {
                esp_peer_disconnect(peer);
                // No main_loop can run after here; close remains on the owner.
                esp_peer_close(peer); peer = nullptr;
            }
            owner_stack = uxTaskGetStackHighWaterMark(nullptr);
            active_peer_trace=nullptr;
            phase("owner-exit");
        }
        void tx_run() {
            try {
                if(!encoder)encoder = std::make_unique<OpusEncoder>(options.opus_bitrate, options.opus_complexity, options.opus_fec, options.opus_dtx, options.uplink_frame_ms);
                emit_encoder_settings(encoder->settings());
                PsBuffer<PcmFrame> pcm;
                std::unique_ptr<PsBuffer<UplinkFrame>> uplink;
                if (options.uplink_frame_ms == 40) uplink=std::make_unique<PsBuffer<UplinkFrame>>();
                PsBuffer<Packet> packet;
                while (!stop) {
                    bool got;
                    { Critical lock(pcm_mux); got = framer.pop(*pcm); }
                    if (!got) { sleep_ms(1); continue; }
                    const int16_t* input=pcm.data->samples.data();
                    packet.data->pts = pcm.data->pts_ms;
                    if (uplink) {
                        auto& frame=*uplink->data;
                        auto discarded=frame.discarded;
                        bool ready=frame.push(*pcm, options.uplink_frame_ms);
                        if (frame.discarded != discarded) drop(UplinkGap, pcm.data->pts_ms);
                        if (!ready) continue;
                        input=frame.samples.data(); packet.data->pts=frame.pts_ms;
                    }
                    auto codec_begin = esp_timer_get_time();
                    packet.data->size = encoder->encode(input, packet.data->data, 1500);
                    unsigned elapsed = esp_timer_get_time() - codec_begin;
                    encode_us = std::max(encode_us.load(), elapsed);
#if CONFIG_DG_PHASE1_DIAGNOSTICS
                    encode_hist.data->add(elapsed);
#endif
                    packet.data->time_us = esp_timer_get_time();
                    if (!encoded.send(*packet)) drop(EncodedQueue,packet.data->pts);
                    else trace->notify_owner();
                }
            } catch (...) { ++send_errors; fault(); }
            tx_stack = uxTaskGetStackHighWaterMark(nullptr); phase("tx-exit");
        }
        // One consumer decision for one scheduled device interval. No queue
        // draining, payload recopy or policy re-poll on this path.
        int pull(const dg::RenderTick& tick) {
            if (!codec) codec=std::make_unique<OpusDecoder>();

            auto action=playout->pull(tick,pcm_renderer.valid());

            if(action.kind==Playout::Kind::Idle && action.reset_decoder) {
                pcm_renderer.invalidate();codec->reset();
            }
            if(action.kind==Playout::Kind::Wait || action.kind==Playout::Kind::Idle ||
                action.kind==Playout::Kind::Disconnected)return 0;
            pcm_generation=action.generation;
            if(action.reset_decoder) {
                trace->record({esp_timer_get_time(),uint32_t(action.timestamp),action.ssrc,
                    action.reset_retired,uint32_t(action.previous_watermark),0,0,
                    PeerTrace::Reset,action.reset_reason,0});
            }
            auto begin=esp_timer_get_time();
            auto result=pcm_renderer.render(action,*codec,remainder.pcm);
            playout->finish(action,result.decoded); // release even when reset/decode failed
            if(result.error)++decode_errors;
            if(action.kind==Playout::Kind::Decode || action.kind==Playout::Kind::Plc) {
                unsigned elapsed=esp_timer_get_time()-begin;decode_us=std::max(decode_us.load(),elapsed);
#if CONFIG_DG_PHASE1_DIAGNOSTICS
                static constexpr uint8_t quiet[]={0x48,0x07,0xc9,0x79,0xc8,0xc9,0x57,0xc0};
                bool silence=action.kind==Playout::Kind::Decode && action.size==sizeof(quiet) &&
                    !std::memcmp(action.payload,quiet,sizeof(quiet));
                decode_hist.data->add(elapsed);
                if(silence)silence_decode_hist.data->add(elapsed);
                else if(action.kind==Playout::Kind::Decode)speech_decode_hist.data->add(elapsed);
#endif
            }
            if(action.kind==Playout::Kind::Plc)trace->mark(PeerTrace::Plc,uint32_t(action.timestamp/48),action.missing_run);
            if(action.kind==Playout::Kind::Insert || action.skipped) {
                const bool insert=action.kind==Playout::Kind::Insert;
                // The zero-wait deferred logger keeps every correction visible
                // even without the optional PSRAM arrival ring. No console IO.
                dg_deferred_log("PLAYOUT_DRIFT t=%lld render=%lld gen=%u playout=%llu edit=%s ready=%u target=%u sum=%u n=%u persist_us=%lld",
                    (long long)esp_timer_get_time(),(long long)action.render_us,
                    unsigned(generation),(unsigned long long)action.generation,insert?"insert":"drop",
                    action.ready_depth,Playout::target_frames,action.drift_filter_sum,
                    action.drift_filter_count,(long long)action.drift_persistence_us);
                trace->record({esp_timer_get_time(),uint32_t(action.timestamp),action.ssrc,
                    1,unsigned(insert),uint16_t(action.ready_depth),uint16_t(Playout::target_frames),
                    PeerTrace::Drift,1,0});
            }
            if(trace_device_epoch!=tick.device_epoch) {
                trace_device_epoch=tick.device_epoch;
                trace->record({esp_timer_get_time(),uint32_t(tick.device_epoch),0,
                    uint32_t(tick.device_epoch>>32),0,0,0,PeerTrace::DeviceEpoch,0,0});
            }
            if(action.trimmed)trace->mark(PeerTrace::Trim,action.trimmed,uint32_t(action.timestamp));
            trace->record({esp_timer_get_time(),uint32_t(action.timestamp),action.ssrc,
                uint32_t(tick.index),uint32_t(action.generation),uint16_t(action.ready_depth),320,PeerTrace::Render,uint8_t(action.kind),0});
            { Critical lock(stats_mux);
              last_render=tick;last_pull_us=esp_timer_get_time();
              if(!pulled_frames) first_pull_us=last_pull_us;
              ++pulled_frames; }
            decode_stack=uxTaskGetStackHighWaterMark(nullptr);
            if(playout->generation()!=pcm_generation) {pcm_renderer.invalidate();return 0;}
            return 320;
        }
        void rx_run() {
            bool notified_open = false, notified_remote = false;
            unsigned reported_drops = 0;int64_t next_drop_log=0;
            try {
                PsBuffer<char> wire(MessageAdmission::slot_bytes(options.max_message_bytes));
                while (!stop) {
                    sleep_ms(1); // Also reached after malformed/control/heap-rejected messages.
                    // Classify a bounded batch off the native send path; the
                    // existing queue retains its reserved bot-ready/control slot.
                    const auto rx_begin=esp_timer_get_time();
                    for(unsigned i=0;i<2 && esp_timer_get_time()-rx_begin<2000;++i) {
                        // Backpressure, not loss: leave work in the inbound
                        // queue until the dispatch queue can take any message.
                        // Only this task pushes, so room cannot shrink below.
                        {std::lock_guard<std::mutex> lock(messages_mutex);if(!messages.can_push())break;}
                        InboundQueue::Lease lease;
                        {Critical lock(msg_mux);lease=inbound.claim();}
                        if(!lease)break;
                        std::memcpy(wire.data,lease.data,lease.size);
                        auto size=lease.size;auto arrival=lease.at;
                        {Critical lock(msg_mux);inbound.release(lease);}
                        auto text=std::string_view(wire.data,size);
                        const auto nodes=MessageAdmission::nodes(text);
                        if(nodes>MessageAdmission::node_limit){drop(AdmissionShape);continue;}
                        if(!MessageAdmission::memory(heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT),
                                heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT),size,nodes)) {
                            drop(AdmissionHeap);continue;
                        }
                        auto kind=classify_message(text);MessageQueue::Result result;
                        {std::lock_guard<std::mutex> lock(messages_mutex);result=messages.push_classified(text,arrival,kind);}
                        if(result!=MessageQueue::Result::Queued && result!=MessageQueue::Result::ReplacedControl)
                            drop(MessageQueueFull,unsigned(result));
                    }
                    unsigned current_drops = dropped.load();
                    if (current_drops != reported_drops && esp_timer_get_time()>=next_drop_log) {
                        next_drop_log=esp_timer_get_time()+1000000;
                        dg_deferred_log("Queue/invalid-frame drops=%u", current_drops);
                        reported_drops = current_drops;
                    }
                    if (opened && !notified_open) {
                        notified_open = true; observer->on_bot_connected({"bot", "", false});
                    }
                    if (remote && !notified_remote) {
                        notified_remote = true;
                        if (notified_open) {
                            observer->on_bot_disconnected({"bot", "", false});
                            observer->on_transport_disconnected();
                        }
                    }
                    // One DOM/SDK handoff per pass. Waiting keeps the bounded wire queue;
                    // native admission counts capacity overflow while callbacks catch up.
                    if(messages.depth() && !observer->can_accept_message())++sdk_backpressure;
                    if(!stop && messages.depth() && observer->can_accept_message()) {
                    size_t size; int64_t arrival;
                    bool got;
                    { std::lock_guard<std::mutex> lock(messages_mutex); got = messages.pop(wire.data, MessageAdmission::slot_bytes(options.max_message_bytes), size, arrival); }
                    if (!got) {sleep_ms(1);continue;}
                    if(!MessageAdmission::memory(heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT),
                            heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT),size,MessageAdmission::nodes({wire.data,size}))) {
                        drop(AdmissionHeap);sleep_ms(1);continue;
                    }
                    try {
                    auto message = nlohmann::json::parse(wire.data, wire.data + size, nullptr, false);
                    if (message.is_discarded()) { drop(MessageParse,size); continue; }
                    if (message.value("type", "") == "signalling") {
                        const auto& m = message.contains("message")?message.at("message"):message;
                        if (m.is_object() && m.value("type", "") == "peerLeft") retire_media();
                        continue;
                    }
                    if (message.value("type", "") == "server-message") {
                        const auto& d = message.contains("data")?message.at("data"):message;
                        if (d.is_object() && d.value("t", "") == "rtp-stats") {
                            const auto& report = d.at("d");
                            server_received = report.at("received").get<unsigned>();
                            server_lost = report.at("lost").get<unsigned>();
                            server_max_gap_ms = report.at("max_gap_ms").get<unsigned>();
                            server_reported = true;
                        }
                    }
                    const bool ping=message.value("type", "")=="server-message" &&
                        message.contains("data") && message["data"].is_object() && message["data"].value("t", "")=="ping";
                    if(!ping)ESP_LOGI(TAG,"DATA_DISPATCH device_us=%lld gen=%u count=%u type=%s bytes=%u",
                        (long long)esp_timer_get_time(),generation,data_dispatched.load()+1,
                        message.value("type", "").c_str(),unsigned(size));
                    if(interrupt_sequence.duplicate(message)) {
                        ++interrupt_duplicates;
                        ESP_LOGI(TAG,"INTERRUPT_DUPLICATE device_us=%lld gen=%u",(long long)esp_timer_get_time(),generation);
                    } else observer->on_transport_message(message);
                    ++data_dispatched;
                    } catch(const nlohmann::json::exception&) {
                        // A syntactically valid but wrongly typed protocol field
                        // is an admission loss, not a worker/session failure.
                        drop(MessageParse,size);
                    }
                    }
                }
            } catch (...) { drop(WorkerException); fault(); }
            rx_stack = uxTaskGetStackHighWaterMark(nullptr); phase("rx-exit");
        }
    };
    EspPeerTransportOptions options;
    TransportObserver* observer = nullptr;
    std::optional<APIRequest> start;
    mutable std::mutex mutex;
    std::shared_ptr<Session> session;
    std::string last_stats = "{}";
    std::shared_ptr<ArrivalRing> trace_ring=std::make_shared<ArrivalRing>();
    uint32_t generation = 0;
    std::atomic<bool> fault_next_channel{false};
    std::unique_ptr<PersistentWorker> owner_worker, tx_worker, rx_worker;
    explicit Impl(EspPeerTransportOptions o) : options(o) {
#if CONFIG_DG_REUSE_TRANSPORT_WORKERS
        owner_worker=std::make_unique<PersistentWorker>("peer-loop",CONFIG_DG_PEER_OWNER_STACK_BYTES,6,0);
        tx_worker=std::make_unique<PersistentWorker>("audio-tx",CONFIG_DG_AUDIO_TX_STACK_BYTES,11,CONFIG_DG_AUDIO_TX_CORE);
        rx_worker=std::make_unique<PersistentWorker>("peer-rx",CONFIG_DG_PEER_RX_STACK_BYTES,7,1,rx_stack_caps);
#endif
    }
    std::shared_ptr<Session> current() const { std::lock_guard<std::mutex> lock(mutex); return session; }
    static StatJson snapshot(Session& s) {
        StatJson payload_sizes=StatJson::object();
        static const char* reason_names[]={"invalid_data","message_queue",
            "encoded_queue","message_parse","event_queue","invalid_sdp","offer_queue","worker_exception","outbound_queue","uplink_gap","send_cutoff","admission_budget","admission_shape","admission_heap","admission_overflow"};
        StatJson drop_reasons=StatJson::object();
        for(unsigned i=0;i<Session::DropReasonCount;++i)drop_reasons[reason_names[i]]=s.drop_reasons[i].load();
        for (unsigned bytes=1;bytes<=1500;++bytes) {
            unsigned count=(*s.payload_sizes)[bytes].load();
            if (count) payload_sizes[StatString(std::to_string(bytes))]=count;
        }
        auto playout=s.playout->snapshot(esp_timer_get_time());auto p=playout.counters;
        unsigned depth=playout.occupancy,delay=Playout::target_frames;
        int64_t age=playout.age_us/1000,span;uint64_t pulls;dg::RenderTick clock;
        {Critical lock(s.stats_mux);span=s.last_pull_us-s.first_pull_us;pulls=s.pulled_frames;clock=s.last_render;}
        auto late=p.rejected[unsigned(IngressError::Late)];
        uint64_t rejected=0;StatJson rejection=StatJson::object();
        static constexpr const char* reasons[]={"none","malformed","payload_type","duration","source","preconnect","late","duplicate","window","capacity","generation","conflict"};
        for(unsigned i=1;i<unsigned(IngressError::Count);++i) {rejected+=p.rejected[i];rejection[reasons[i]]=p.rejected[i];}
        size_t msg_depth, pcm_depth; uint64_t pcm_drops; int64_t msg_age;
        { std::lock_guard<std::mutex> lock(s.messages_mutex); msg_depth = s.messages.depth(); msg_age = s.messages.oldest_age_ms(now_ms()); }
        { Critical lock(s.pcm_mux); pcm_depth = s.framer.depth(); pcm_drops = s.framer.dropped(); }
        // Build one field at a time: the complete schema initializer kept
        // every JSON temporary live on the unchanged console stack.
        StatJson result=StatJson::object();
        result["connect_timings"]={{"open_us",s.open_us.load()},{"offer_us",s.offer_us.load()},
                {"answer_applied_us",s.answer_us.load()},{"connected_us",s.connected_us.load()},{"channel_open_us",s.channel_us.load()}};
        result["peer_profile"]=s.trace->stats();
        result["generation"]=s.generation;
        result["pc_id"]=s.pc_id;
        result["sent"]=s.sent.load();
        result["received"]=s.packets.load();
        result["received_payload_size_histogram"]=std::move(payload_sizes);
        result["preconnect_packets"]=s.preconnect_packets.load();
        result["rtp_before_chat_packets"]=s.rtp_before_chat_packets.load();
        result["first_rtp_before_chat_us"]=s.first_rtp_before_chat_us.load();
        result["playout_schema"]=2;
        result["playout"]={{"ingress",p.ingress},{"accepted",p.accepted},{"rejected",rejection},
                {"missing_slots",p.missing_slots},{"plc_calls",p.plc_calls},{"missing_run_ended",p.missing_run_ended},{"post_plc_zeros",p.post_plc_zeros},
                {"decode_errors",p.decode_errors},{"starvation",p.starvation},{"drift_drop",p.drift_drop},
                {"drift_insert",p.drift_insert},{"latency_trim",p.latency_trim},{"discontinuity",p.discontinuity},
                {"ts_gap",p.ts_gap},{"decoded_packets",p.decoded_packets},{"render_decisions",p.render_decisions},
                {"device_silence_blocks",p.device_silence_blocks},{"media_advance",p.media_advance},{"tick_resync",p.tick_resync},
                {"stream_generation",playout.generation},{"state",unsigned(playout.state)},
                {"watermark",playout.watermark},{"render_tick",playout.render_tick},
                {"physical_occupancy",depth},{"ready_depth",playout.ready_depth},
                {"ready_high_water",playout.ready_high_water},{"future_span_frames",playout.future_span},
                {"target_ms",Playout::target_frames*20},{"metadata_bytes",sizeof(Playout)},{"payload_bytes",sizeof(Playout::Storage)},{"native_shadow_budget_bytes",16384},{"clock",{{"device_generation",clock.device_epoch},
                {"submitted_samples",clock.submitted_samples},{"completed_samples",clock.completed_samples},
                {"device_silence_samples",clock.silence_samples},{"partial_remainder",clock.remainder_samples}}}};
        result["cancellation"]={{"events",p.cancellations},{"duplicate_notices",s.interrupt_duplicates.load()},
            {"packets",p.cancelled_packets},{"render_intervals",p.cancelled_render_intervals},
            {"pcm_samples",s.cancelled_pcm_samples.load()},{"mute_ms",150}};
        result["post_plc_zeros"]=p.post_plc_zeros;
        result["device_silence_blocks"]=p.device_silence_blocks;
        result["discontinuity"]=p.discontinuity;
        result["latency_trim"]=p.latency_trim;
        result["dropped"]=s.dropped.load() + pcm_drops + rejected;
        result["late"]=late;
        result["lost"]=p.missing_slots;
        result["concealed"]=p.plc_calls;
        result["queue_dropped"]=s.dropped.load() + pcm_drops + p.rejected[unsigned(IngressError::Capacity)];
        result["playout_dropped"]=rejected;
        result["drop_reasons"]=std::move(drop_reasons);
        result["pcm_queue_drops"]=pcm_drops;
        result["playout_overflow"]=p.rejected[unsigned(IngressError::Capacity)];
        result["closing_control_sent"]=s.closing_control_sent.load();
        result["send_budget_cutoffs"]=s.send_budget_cutoffs.load();
        result["stale_audio_skipped"]=s.stale_audio_skipped.load();
        result["closing_flush_timeout"]=s.closing_flush_timeout.load();
        result["decode_errors"]=s.decode_errors.load();
        result["send_errors"]=s.send_errors.load();
        result["mic_in_turn_clips"]=dg_audio_mic_in_turn_clips ? dg_audio_mic_in_turn_clips() : 0;
        if(dg_audio_mic_gate_open_clips && dg_audio_mic_gate_closed_clips) {
            result["mic_gate_open_clips"]=dg_audio_mic_gate_open_clips();
            result["mic_gate_closed_clips"]=dg_audio_mic_gate_closed_clips();
        }
        result["output_volume_db"]=dg_audio_output_volume_db ? dg_audio_output_volume_db() : 0;
        result["i2s_write_errors"]=dg_audio_i2s_write_errors ? dg_audio_i2s_write_errors() : 0;
        result["i2s_write_timeouts"]=dg_audio_i2s_write_timeouts ? dg_audio_i2s_write_timeouts() : 0;
        result["i2s_underflows"]=dg_audio_i2s_underflows ? dg_audio_i2s_underflows() : 0;
        result["playback_stack_bytes"]=CONFIG_DG_PLAYBACK_STACK_BYTES;
        result["bytes_sent"]=s.bytes_sent.load();
        result["bytes_received"]=s.bytes_received.load();
        result["pcm_queue"]=pcm_depth;
        result["encoded_queue"]=s.encoded.depth();
        result["message_queue"]=msg_depth;
        result["message_age_ms"]=msg_age;
        result["playout_queue"]=depth;
        result["playout_age_ms"]=age;
        result["playout_target_ms"]=delay*20;
        result["target_depth"]=delay;
        result["current_depth"]=depth;
        result["pulled_frames"]=pulls;
        result["pull_span_us"]=span;
        result["prefills"]=p.prefills;
        result["rebuffers"]=p.starvation;
        result["drift_drops"]=p.drift_drop;
        result["drift_inserts"]=p.drift_insert;
        result["plc_frames"]=p.plc_calls;
        result["enqueue_send_max_us"]=s.latency_max_us.load();
        result["agent_recv_timeout_ms"]=s.options.agent_recv_timeout_ms;
        result["loop_delay_ms"]=s.options.loop_delay_ms;
        result["enqueue_send_mean_us"]=s.latency_total_us.load() / std::max(1u, s.latency_count.load());
        result["loop_max_us"]=s.loop_max_us.load();
        result["udp_empty_suppressed"]=pipecat_empty_udp_count();
        result["server_loss_known"]=s.server_reported.load();
        result["server_received"]=s.server_received.load();
        result["server_lost"]=s.server_lost.load();
        result["server_max_gap_ms"]=s.server_max_gap_ms.load();
        result["dtls_duration_us"]=dg_dtls_duration_us();
        result["encode_frames"]=s.encode_hist.data->count.load();
        result["decode_frames"]=s.decode_hist.data->count.load();
        result["encode_p50_us"]=s.encode_hist.data->quantile(50);
        result["encode_p95_us"]=s.encode_hist.data->quantile(95);
        result["decode_p50_us"]=s.decode_hist.data->quantile(50);
        result["decode_p95_us"]=s.decode_hist.data->quantile(95);
        result["silence_decode_frames"]=s.silence_decode_hist.data->count.load();
        result["silence_decode_p50_us"]=s.silence_decode_hist.data->quantile(50);
        result["silence_decode_p95_us"]=s.silence_decode_hist.data->quantile(95);
        result["speech_decode_frames"]=s.speech_decode_hist.data->count.load();
        result["speech_decode_basis"]="Valid decode calls excluding the exact measured quiet packet; includes transitions; PLC excluded";
        result["speech_decode_p50_us"]=s.speech_decode_hist.data->quantile(50);
        result["speech_decode_p95_us"]=s.speech_decode_hist.data->quantile(95);
        result["opus_bitrate"]=s.options.opus_bitrate;
        result["jitter_cache_timeout_ms"]=s.options.jitter_cache_timeout_ms;
        result["uplink_frame_ms"]=s.options.uplink_frame_ms;
        result["opus_dtx"]=s.options.opus_dtx;
        result["opus_fec"]=s.options.opus_fec;
        result["owner_priority"]=s.options.owner_priority;
        result["direct_socket_select"]=s.options.direct_socket_select;
        result["event_wake"]=s.options.event_wake;
        result["data_admission"]={{"received",s.data_received.load()},{"dispatched",s.data_dispatched.load()},{"sdk_backpressure",s.sdk_backpressure.load()},{"wire_limit",MessageAdmission::wire_limit},
            {"deferred",s.data_deferred.load()},{"deferred_depth",s.deferred_depth.load()},{"deferred_max",s.deferred_max.load()},
            {"capacity",InboundQueue::capacity}};
        result["receive_budget_after_read"]=s.options.receive_budget_after_read;
        result["eager_codec_initialization"]=s.options.eager_codec_initialization;
        result["full_call_timing"]=s.options.full_call_timing;
        result["arrival_trace"]=s.options.arrival_trace;
        result["codec_histogram_bucket_us"]=TimingHistogram::width;
        result["encode_max_us"]=s.encode_us.load();
        result["decode_max_us"]=s.decode_us.load();
        result["tx_stack"]=s.tx_stack.load();
        result["decode_stack"]=s.decode_stack.load();
        result["rx_stack"]=s.rx_stack.load();
        result["owner_stack"]=s.owner_stack.load();
        result["tx_stack_bytes"]=CONFIG_DG_AUDIO_TX_STACK_BYTES;
        result["owner_stack_bytes"]=CONFIG_DG_PEER_OWNER_STACK_BYTES;
        result["rx_stack_bytes"]=CONFIG_DG_PEER_RX_STACK_BYTES;
        result["tx_core"]=CONFIG_DG_AUDIO_TX_CORE;
        result["tasks_exited"]=s.jobs_exited.load();
        return result;
    }
};
EspPeerTransport::EspPeerTransport(EspPeerTransportOptions o) : impl_(std::make_unique<Impl>(o)) {
    if (o.sample_rate != 16000 || o.channels != 1 || !o.max_message_bytes || o.max_message_bytes > 16384 ||
        o.opus_complexity > 10 || (o.uplink_frame_ms != 20 && o.uplink_frame_ms != 40) ||
        o.connect_timeout.count() <= 0 || o.agent_recv_timeout_ms < 1 || o.agent_recv_timeout_ms > 100 ||
        o.owner_priority < 1 || o.owner_priority >= 18 || o.loop_delay_ms > 20 ||
        o.jitter_cache_timeout_ms < 20 || o.jitter_cache_timeout_ms > 300)
        throw InvalidTransportParamsError("Unsupported ESP peer options");
    if(o.playout_target_frames!=7 || o.playout_backlog_frames!=10)
        throw InvalidTransportParamsError("Playout requires fixed 140 ms target and 200 ms backlog cap");
}
EspPeerTransport::~EspPeerTransport() { disconnect(); }
void EspPeerTransport::initialize(TransportObserver* observer) {
    if(impl_->current())throw TransportStartError("Cannot replace an active transport observer");
    impl_->observer = observer;
}
void EspPeerTransport::set_start_bot_params(const APIRequest& request) {
    impl_->start=request;
    // Exactly the HTTP body, without endpoint, headers or response credentials.
    if(wire_start_safe(request.request_data))wire_emit("start",request.request_data);
    else {
        try {evidence_line("WIRE_CONTRACT_REFUSED start_keys="+wire_start_keys(request.request_data).dump()+"\n");}
        catch(...) {dg_deferred_log("WIRE_CONTRACT ERROR");}
    }
}
void EspPeerTransport::fault_next_channel_open() { impl_->fault_next_channel=true; }
void EspPeerTransport::diagnostic_option(const std::string& name,unsigned value) {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if(impl_->session)throw InvalidTransportParamsError("Options require disconnected transport");
    auto& o=impl_->options;
    if(name=="bitrate" && (value==16000 || value==24000 || value==32000))o.opus_bitrate=value;
    else if(name=="delay" && value<=20)o.loop_delay_ms=value;
    else if(name=="recv" && value>=1 && value<=100)o.agent_recv_timeout_ms=value;
    else if(name=="target" && value==7)o.playout_target_frames=value;
    else if(name=="jitter" && (value==60 || value==100 || value==160 || value==200 || value==300))o.jitter_cache_timeout_ms=value;
    else if(name=="aes-gcm" && value<=1)o.dtls_aes_gcm=value;
    else if(name=="priority" && (value==6 || value==8 || value==14))o.owner_priority=value;
    else if(name=="frame-ms" && (value==20 || value==40))o.uplink_frame_ms=value;
    else if(name=="dtx" && value<=1)o.opus_dtx=value;
    else if(name=="codec-eager" && value<=1)o.eager_codec_initialization=value;
    else if(name=="receive-after-read" && value<=1)o.receive_budget_after_read=value;
    else if(name=="fec" && value<=1)o.opus_fec=value;
    else if(name=="socket-select" && value<=1)o.direct_socket_select=value;
    else if(name=="event-wake" && value<=1)o.event_wake=value;
    else if(name=="call-timing" && value<=1)o.full_call_timing=value;
    else if(name=="arrival-trace" && value<=1) {if(value)impl_->trace_ring->enable();o.arrival_trace=value;}
    else throw InvalidTransportParamsError("Unknown or invalid diagnostic option");
}
void EspPeerTransport::connect(const nlohmann::json& params) {
    const int64_t began = now_ms();
    if (!impl_->observer) throw TransportStartError("Transport not initialized");
    if (impl_->current()) throw TransportStartError("Transport already connected");
    {std::lock_guard<std::mutex> lock(impl_->mutex);std::string("{}").swap(impl_->last_stats);}
    auto parsed = parse_connection_params(params, impl_->start);
    auto options=impl_->options;
    if(options.arrival_trace)impl_->trace_ring->enable();
    const bool injected_fault=impl_->fault_next_channel.exchange(false);
    if(injected_fault)options.diagnostic_channel_timeout=true;
    options.connect_timeout=esp_peer::connect_deadline(options.connect_timeout,
        parsed.offer_url.rfind("https://",0)==0,injected_fault);
    const int64_t deadline = began + options.connect_timeout.count();
    try {
        auto s = std::make_shared<Impl::Session>(options, impl_->observer, ++impl_->generation,impl_->trace_ring);
        s->trace->connect_deadline_us=deadline*1000;
        s->trace->stopping=&s->stop;
        s->connection = std::move(parsed);
        auto& connection = s->connection;
        { std::lock_guard<std::mutex> lock(impl_->mutex); impl_->session = s; }
        auto& extra = s->extra;
        extra.agent_recv_timeout = s->options.agent_recv_timeout_ms;
        extra.data_ch_cfg.send_cache_size = extra.data_ch_cfg.recv_cache_size = 32768;
        extra.data_ch_cfg.cache_timeout = 5000;
        extra.rtp_cfg.audio_recv_jitter.cache_size = 16384;
        extra.rtp_cfg.audio_recv_jitter.cache_timeout = s->options.jitter_cache_timeout_ms;
        extra.rtp_cfg.send_pool_size = 32768; extra.rtp_cfg.send_queue_num = 64; extra.rtp_cfg.max_resend_count = 3;
        extra.alive_binding_retries = 5;
        auto& cfg = s->config;
        cfg.role = ESP_PEER_ROLE_CONTROLLING;
        cfg.audio_info.codec = ESP_PEER_AUDIO_CODEC_OPUS; cfg.audio_info.sample_rate = 16000; cfg.audio_info.channel = 1;
        cfg.audio_dir = ESP_PEER_MEDIA_DIR_SEND_RECV;
        cfg.enable_data_channel = true; cfg.manual_ch_create = true; cfg.no_auto_reconnect = true;
        cfg.extra_cfg = &extra; cfg.extra_size = sizeof(extra); cfg.ctx = s.get();
        cfg.on_state = Impl::Session::state; cfg.on_msg = Impl::Session::local_sdp;
        cfg.on_audio_info = Impl::Session::audio_info; cfg.on_audio_data = Impl::Session::audio_data;
        cfg.on_channel_open = Impl::Session::channel_open; cfg.on_channel_close = Impl::Session::channel_close; cfg.on_data = Impl::Session::data;
        auto& servers = s->servers;
        for (auto& ice : connection.ice_servers) {
            esp_peer_ice_server_cfg_t entry{};
            entry.stun_url = const_cast<char*>(ice.url.c_str());
            // Native agent selects TURN allocation when credentials are non-null.
            // Plain STUN must use null pointers, rather than empty strings.
            entry.user = ice.username.empty()?nullptr:const_cast<char*>(ice.username.c_str());
            entry.psw = ice.password.empty()?nullptr:const_cast<char*>(ice.password.c_str());
            servers.push_back(entry);
        }
        if (servers.size() > 255) throw InvalidTransportParamsError("Too many ICE servers");
        cfg.server_lists = servers.data(); cfg.server_num = servers.size();
        esp_peer_set_dtls_cipher_pref(s->options.dtls_aes_gcm?ESP_PEER_DTLS_CIPHER_AES_GCM:ESP_PEER_DTLS_CIPHER_AUTO);
        if (esp_peer_open(&cfg, esp_peer_get_default_impl(), &s->peer)) throw TransportStartError("Peer open failed");
        s->open_us=esp_timer_get_time();
        phase("open");
        [[maybe_unused]] auto launch = [&](const char* name, unsigned stack, unsigned priority, int core, auto method,
                                          unsigned caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) {
            dg::PthreadConfig threads;auto thread_cfg = threads.current();
            thread_cfg.thread_name = name; thread_cfg.stack_size = stack; thread_cfg.prio = priority; thread_cfg.pin_to_core = core;
            // Keep hot codec scratch and the owner internal. The RX worker
            // parses JSON and posts observer events; it does no codec work.
            thread_cfg.stack_alloc_caps = caps;
            threads.set(thread_cfg);
            return std::thread([s, method] { (s.get()->*method)(); });
        };
        {
            #if CONFIG_DG_REUSE_TRANSPORT_WORKERS
            impl_->owner_worker->start(s.get(), [](void* p){static_cast<Impl::Session*>(p)->owner_run();}); s->owner_started=true;
            impl_->tx_worker->start(s.get(), [](void* p){static_cast<Impl::Session*>(p)->tx_run();});
            impl_->rx_worker->start(s.get(), [](void* p){static_cast<Impl::Session*>(p)->rx_run();});
#else
            s->owner = launch("peer-loop", CONFIG_DG_PEER_OWNER_STACK_BYTES, options.owner_priority, 0, &Impl::Session::owner_run); s->owner_started=true;
            s->tx = launch("audio-tx", CONFIG_DG_AUDIO_TX_STACK_BYTES, 11, CONFIG_DG_AUDIO_TX_CORE, &Impl::Session::tx_run);
            s->rx = launch("peer-rx", CONFIG_DG_PEER_RX_STACK_BYTES, 7, 1, &Impl::Session::rx_run, rx_stack_caps);
#endif
        }
        auto check_deadline = [&] {
            if (now_ms() >= deadline) throw TransportStartError("Shared connect deadline expired");
            if (s->failed || s->remote) throw TransportStartError("Peer failed during connect");
        };
        PsBuffer<Text> text;
        while (!s->offer.recv(*text, 10)) check_deadline();
        s->offer_us=esp_timer_get_time();
        check_deadline();
        std::string offer(text.data->data, text.data->size);
        rewrite_aiortc_active_offer(offer);
        wire_emit("sdp",wire_sdp(offer));
        APIRequest request;
        request.endpoint = connection.offer_url; request.headers = connection.headers;
        request.request_data = {{"sdp", offer}, {"type", "offer"}};
        if (!connection.request_data.is_null()) request.request_data["requestData"] = connection.request_data;
        request.timeout = std::chrono::milliseconds(std::max<int64_t>(1, deadline - now_ms()));
        auto response = esp::http_post(request, [&] { return s->stop.load() || now_ms() >= deadline; });
        check_deadline();
        auto answer = validate_answer(response.status, response.body);
        auto response_json = nlohmann::json::parse(response.body);
        s->pc_id = response_json.value("pc_id", "");
        esp_netif_ip_info_t ip{};
        if (dg_net_ip_info(&ip) != ESP_OK) throw TransportStartError("No station IP");
        auto filtered = filter_remote_candidates(answer, ntohl(ip.ip.addr), ntohl(ip.netmask.addr), signaling_ip(connection.offer_url),
            connection.offer_url.rfind("https://", 0) == 0);
        check_deadline();
        ESP_LOGI(TAG, "CANDIDATES kept=%u dropped=%u", unsigned(filtered.kept), unsigned(filtered.dropped));
        text.data->size = filtered.sdp.size();
        std::memcpy(text.data->data, filtered.sdp.c_str(), filtered.sdp.size() + 1);
        if (!s->answer.send(*text)) throw TransportStartError("Answer queue full");
        s->trace->notify_owner();
        while (!s->opened) {
            if(now_ms()>=deadline)throw TransportStartError("Data-channel-open timeout");
            check_deadline(); sleep_ms(5);
        }
        check_deadline(); phase("channel-open");
    } catch (const std::exception& error) {
        std::string reason=error.what();disconnect();
        throw TransportStartError("ESP peer connect failed; partial session torn down: "+reason);
    } catch (...) {
        disconnect();throw TransportStartError("ESP peer connect failed; partial session torn down");
    }
}
void EspPeerTransport::disconnect() {
    auto s = impl_->current();
    if (!s) return;
    s->freeze_uplink(esp_timer_get_time());s->stop = true;s->playout->disconnect();
    s->trace->notify_owner();if(dg_audio_media_invalidated)dg_audio_media_invalidated();
    ++impl_->generation;
    // Wait for the bounded inline decode before disposing its session state.
    std::unique_lock<std::mutex> reader(s->read_mutex);
    #if CONFIG_DG_REUSE_TRANSPORT_WORKERS
    impl_->tx_worker->wait(); impl_->rx_worker->wait();
#else
    if (s->tx.joinable()) s->tx.join();
    if (s->rx.joinable()) s->rx.join();
#endif
    if(s->owner_started && s->opened && !s->failed) {
        auto deadline=now_ms()+500;
        while(!s->closing_flushed && !s->failed && now_ms()<deadline)sleep_ms(1);
        if(!s->closing_flushed && !s->failed)s->closing_flush_timeout=true;
    }
    s->finish_owner = true;
    #if CONFIG_DG_REUSE_TRANSPORT_WORKERS
    if (s->owner_started) impl_->owner_worker->wait();
#else
    if (s->owner.joinable()) s->owner.join();
#endif
    if (!s->owner_started && s->peer) {
        // Owner creation failed: ownership has never transferred from connect.
        s->owner_task = xTaskGetCurrentTaskHandle();
        esp_peer_close(s->peer); s->peer = nullptr;
    }
    UplinkSnapshot frozen;
    if(s->claim_uplink(frozen)) {
        if(frozen.ok)intervals_emit(frozen);
        else evidence_line("UPLINK_INTERVALS_NONE gen="+std::to_string(s->generation)+" reason=no-measured-frames\n");
    }
    drain_wire_evidence();
    dg_dtls_cancel(); s->jobs_exited=true;
    s->encoder.reset(); s->codec.reset(); s->playout->disconnect();
    std::string stats = "{}";
    try { stats = stat_dump(Impl::snapshot(*s)); } catch (...) {}
    { std::lock_guard<std::mutex> lock(impl_->mutex); impl_->last_stats = std::move(stats); impl_->session.reset(); }
    phase("closed");
}
void EspPeerTransport::send_ready_message(const rtvi::Message& message) { send_message(message); }
void EspPeerTransport::send_message(const rtvi::Message& message) {
    auto wire = nlohmann::json(message).dump();
    check_message_size(wire.size(), impl_->options.max_message_bytes);
    auto s = impl_->current();
    if (!s || s->stop || !s->opened) throw BotNotReadyError();
    PsBuffer<Text> text;
    text.data->size = wire.size(); std::memcpy(text.data->data, wire.data(), wire.size());
    text.data->closing_control=message.type=="disconnect-bot";
    text.data->evidence=wire_message_kind(message.type,message.data);
    if(text.data->evidence!=WireMessage::None &&
       !(s->wire_seen.load(std::memory_order_relaxed)&unsigned(text.data->evidence))) {
        try {
            const auto record=wire_message_record(nlohmann::json(message));
            const auto line="WIRE_CONTRACT "+record.first+(record.first.rfind("msg=",0)==0?" ":"=")+record.second.dump()+"\n";
            if(line.size()>sizeof(text.data->record.data))dg_deferred_log("WIRE_CONTRACT ERROR oversize");
            else {text.data->record.size=line.size();std::memcpy(text.data->record.data,line.data(),line.size());}
        }catch(...) {dg_deferred_log("WIRE_CONTRACT ERROR");}
    }
    if (!s->outbound.send(*text)) { s->drop(Impl::Session::OutboundQueue); throw PipecatError("Outgoing message queue full"); }
    s->trace->notify_owner();
}
size_t EspPeerTransport::max_message_size() const { return impl_->options.max_message_bytes; }
int32_t EspPeerTransport::send_user_audio(const int16_t* frames, size_t count) {
    auto s = impl_->current();
    if (!s || s->stop || !s->opened) return 0;
    Critical lock(s->pcm_mux);
    if(!frames && !count) {s->framer.discontinuity();return 0;}
    return s->stop ? 0 : s->framer.push(frames, count);
}
int32_t EspPeerTransport::read_bot_audio(int16_t* frames, size_t count) {
    dg::MediaToken token;return read_bot_audio(frames,count,token);
}
int32_t EspPeerTransport::read_bot_audio(int16_t* frames, size_t count,dg::MediaToken& token) {
    token={};
    auto s = impl_->current();
    if (!s || s->stop || !count || !frames) return 0;
    std::lock_guard<std::mutex> lock(s->read_mutex);
    if(s->stop || s->remote || s->failed) return 0;
    const auto cancel_serial=s->cancel_serial.load();
    if(cancel_serial!=s->consumed_cancel_serial) {
        s->cancelled_pcm_samples+=size_t(s->remainder.count)-s->offset;
        s->offset=s->remainder.count=0;s->pcm_renderer.invalidate();
        // The next valid pull carries reset_decoder, including muted/idle pulls.
        s->consumed_cancel_serial=cancel_serial;
    }
    const auto tick=s->options.render_clock?s->options.render_clock():dg::RenderTick{};
    // A partial caller may retain only the suffix of the same device interval.
    // A new tick expires it without replaying PCM or advancing twice.
    if(s->remainder.count && (!tick.valid || tick.device_epoch!=s->last_render.device_epoch ||
        tick.index!=s->last_render.index))s->offset=s->remainder.count=0;
    if(s->pcm_generation!=s->playout->generation()) {
        s->offset=s->remainder.count=0;s->pcm_renderer.invalidate();
    }
    if(s->offset==size_t(s->remainder.count)) {
        try {s->remainder.count=s->pull(tick);}
        catch(...) {++s->decode_errors;s->fault();return 0;}
        s->offset=0;
    }
    if(s->stop || !s->remainder.count) return 0;
    size_t n = std::min(count, size_t(s->remainder.count) - s->offset);
    std::copy_n(s->remainder.pcm + s->offset, n, frames); s->offset += n;
    token={s,s->pcm_generation};
    return n;
}
bool EspPeerTransport::cancel_bot_audio(int64_t now_us) {
    auto s=impl_->current();if(!s || s->stop)return false;
    // Serialize cancellation metadata with partial-PCM consumption. Codec and
    // DSP resets remain on the playback owner, never this control caller.
    std::lock_guard<std::mutex> reader(s->read_mutex);
    if(!s->playout->cancel(now_us))return false;
    ++s->cancel_serial;return true;
}
void EspPeerTransport::resume_bot_audio() {
    auto s=impl_->current();if(s && !s->stop)s->playout->resume();
}
bool EspPeerTransport::media_ready() const {
    auto s=impl_->current();return s && !s->stop && !s->remote && !s->failed && s->playout->ready();
}
nlohmann::json EspPeerTransport::stats() const {
    return nlohmann::json::parse(stats_dump());
}
std::string EspPeerTransport::stats_dump() const {
    auto s = impl_->current();
    if (s) return stat_dump(Impl::snapshot(*s));
    std::lock_guard<std::mutex> lock(impl_->mutex); return impl_->last_stats;
}
void EspPeerTransport::freeze_uplink() {
    if(auto s=impl_->current())s->freeze_uplink(esp_timer_get_time());
}
void EspPeerTransport::dump_uplink_intervals() {
    auto s=impl_->current();
    if(!s){evidence_line("UPLINK_INTERVALS_NONE reason=no-session\n");return;}
    UplinkSnapshot frozen;
    if(!s->claim_uplink(frozen))evidence_line("UPLINK_INTERVALS_NONE gen="+std::to_string(s->generation)+" reason=already-reported\n");
    else if(!frozen.ok)evidence_line("UPLINK_INTERVALS_NONE gen="+std::to_string(s->generation)+" reason=no-measured-frames\n");
    else intervals_emit(frozen);
}
void EspPeerTransport::drain_wire_evidence() {
    auto s=impl_->current();if(!s)return;
    WireEvidence record;
    while(s->wire_evidence.recv(record)) {
        if(record.size)evidence_line(std::string(record.data,record.size));
        else wire_emit("dc_open",{{"label",record.data},{"local_ordered",record.local_ordered},{"api_protocol_default",""}});
    }
}
size_t EspPeerTransport::drain_trace(void* records,size_t capacity) {
    return impl_->trace_ring->drain(static_cast<ArrivalRecord*>(records),capacity);
}
}
