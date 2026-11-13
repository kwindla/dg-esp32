#pragma once
#include "dg_diagnostics.h"
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_display/dma_watchdog.h"
#include "dg_display/bounded_mutex.h"
#include "dg_display/connection_scope.h"
#include "dg_display/character.h"
#include "dg_display/demo_assets.h"
#include "dg_display/demo_proof.h"
#include "dg_audio/character_signals.h"
#include "dg_display/panel_config.h"
#include "dg_display/runtime.h"
#include "dg_display/r3_stress.h"
#include "dg_display/r5_hash.h"
#include "dg_display/r4_crypto.h"
#include "sdkconfig.h"
#include "dg_display/json_request.h"
#include "dg_display/flash_store.h"
#include "dg_display.h"
#include "esp_heap_caps.h"
#include "esp_random.h"
#include "driver/gpio.h"
#include "esp_http_client.h"
#include "esp_littlefs.h"
#include "esp_partition.h"
#include <fcntl.h>
#include <unistd.h>
#include "esp_private/flash_mmap.h"
#include "esp_rom_crc.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <new>
#include <cstring>
#include <cstdio>
#include <algorithm>
#include <atomic>
#include <stdexcept>
#include "dg_display/present.h"
#if CONFIG_DG_DUO
#include "dg_display/duo_commit.h"
#endif
namespace dg_display {namespace detail {
constexpr unsigned Psram=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT,Internal=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT;
struct Trace {int64_t begin=0,end=0;uint32_t crc=0,number=0;};
constexpr unsigned ErrorCapacity=128,SpiCapacity=128;
struct StripeContext {unsigned frame=0,bytes=0;uint16_t stripe=0,x0=0,y0=0,x1=0,y1=0;uint8_t kind=0,phase=0,spi1=0;};
struct DmaError {int64_t start=0,completion=0,recycle=0;unsigned flags=0,overlap[3]{};StripeContext context;};
struct SpiTrace {std::atomic<int64_t> start{0},end{0};std::atomic<unsigned> token{0},op{0},address{0},bytes{0};std::atomic<int> error{0};};
struct FlashTrace {std::atomic<int64_t> requested{0},start{0},end{0};std::atomic<unsigned> op{0};std::atomic<int> error{0};};
struct FlashMetric {std::atomic<uint64_t> count{0},total{0},maximum{0};std::atomic<unsigned> bins[32]{};};
struct FrameTrace {int64_t start=0,end=0,wait=0,hold=0;unsigned generation=0;bool success=false;};
struct StoreCase {char id[33]{};int64_t posted=0,completed=0;Error error=Error::None;RequestKind kind=RequestKind::Persist;uint32_t bytes=0;};
struct DemoProof {uint64_t number=0,revision=0,rendered=0;int64_t completed=0;unsigned screen=0;};
struct Persisted {char hash[65]{};Error error=Error::None;uint64_t number=0;};
void state_represent();
struct State {
    std::atomic<bool> dead{false};
    std::atomic<unsigned> terminal_reason{0},scheduler_delays{0},lock_timeouts{0},lock_failures{0};
    std::atomic<bool> engine_lock_failed{false};
    std::atomic<int64_t> io_started{0},stripe_start{0},stripe_completion{0};
    std::atomic<bool> color_inflight{false},dma_inflight{false};
    std::atomic<int64_t> dma_started{0},dma_ended{0};
    // Valid in PSRAM only while XIP/cache stays enabled and LCD ISR is non-IRAM.
    StaticSemaphore_t gate_storage;SemaphoreHandle_t gate=nullptr;
    std::atomic<bool> display_token{false},writer_waiting{false},display_waiting{false};
    bool writer_token=false;std::atomic<unsigned> gate_timeouts{0},display_gate_timeouts{0};
    unsigned stripes_while_writer_waits=0,ops_while_display_waits=0;std::atomic<unsigned> max_stripes_while_writer_waits{0},max_ops_while_display_waits{0};
    int64_t frame_gate_wait=0;std::atomic<int64_t> frame_gate_hold{0};
    FlashMetric gate_wait_metrics[4]{},gate_hold_metrics[4]{},frame_gap_metrics;
    FrameTrace frame_trace[128]{};unsigned frame_count=0;int64_t prior_frame_start=0;
    std::atomic<bool> diagnostic_boot{false},gate_bypass{false};
    std::atomic<unsigned> r2_fault{0},late_callbacks{0},send_failures{0};
    std::atomic<int64_t> late_due{0},held_gate_due{0};TaskHandle_t display_owner=nullptr;
    FlashTrace flash_trace[1024]{};std::atomic<unsigned> flash_count{0};
    FlashMetric flash_metrics[3]{};unsigned error_buckets[8]{};
    std::atomic<bool> metadata_walk{false},metrics_reset{false},metrics_reset_done{false};
    TaskHandle_t store_owner=nullptr;
    StripeContext stripe_context;unsigned next_frame=0;
    SpiTrace spi_trace[SpiCapacity]{};std::atomic<unsigned> spi_count{0},spi_coverage_errors{0},spi_active[3]{};
    std::atomic<unsigned> stripe_spi_mask{0};
    std::atomic<uint64_t> stripes_attempted{0},stripes_queued{0},stripes_completed{0},bytes_attempted{0},bytes_completed{0},dma_active_us{0},scheduling_gap_max_us{0};
    std::atomic<int64_t> last_color_done{0};
    DmaError dma_error_ring[ErrorCapacity]{};unsigned dma_error_count=0;
    unsigned display_recovery_attempts=0,display_recoveries=0;bool recovery_pending=false;
    esp_timer_handle_t watchdog=nullptr;
    Cache cache;FakeClock clock;Engine engine{cache,clock};FlashStore flash;
    FlashItem inventory[128]{};uint64_t flash_revision=1;size_t fs_capacity=0,fs_used=0,fs_free=0,fs_temp=0,fs_admissible=0;unsigned fs_count=0;bool fs_ready=false;
    Persisted persisted[32]{};uint64_t persisted_count=0;
    char store_assets[5][65]{};StoreCase store_cases[9]{};Request store_request;unsigned store_step=0,store_generation=0;int64_t store_start=0;bool store_bench=false;
    std::atomic<bool> session_active{false},allow_call_persistence{false},defer_persistence{false},provision{false},provision_done{false};
    std::atomic<unsigned> fault_stage{0},fault_reached{0};Error provision_error=Error::None;
    StaticSemaphore_t lock_storage;SemaphoreHandle_t lock=nullptr;
    esp_lcd_panel_handle_t panel;esp_lcd_panel_io_handle_t io;uint8_t* staging;size_t staging_bytes;
    std::atomic<unsigned> wire_received{0},wire_accepted{0},wire_busy{0},wire_queue_full{0},wire_invalid{0},wire_wait_max_us{0};
    std::atomic<unsigned> ack_sent{0},ack_queue_full{0},ack_send_failed{0},ack_stale{0};
    unsigned wire_wait_ms=5;uint64_t bench_sequence=0;
    LogicalAdapter adapter;
    Presenter presenter{adapter,[](void*){state_represent();return PresentError::None;}};
    // One bounded mailbox. Context ownership stays with the sender until the
    // noexcept command returns on display_owner; no engine lock is held there.
    std::atomic<unsigned> owner_command_state{0};DisplayOwnerCommand owner_command;
    State(uint16_t* native_scratch,size_t native_pixels):adapter(panel_backend(),
        {PanelWidth,PanelHeight,dg_board::CanvasScale,dg_board::CanvasOffsetX,dg_board::CanvasOffsetY,0},native_scratch,native_pixels){}
    Pixel scratch[PanelWidth]{};uint8_t probe_buffer[4096]{};char probe_url[513]{};std::atomic<bool> probe_pending{false};RenderLease lease;Request work,console_request,bench_request;
    esp_http_client_handle_t http=nullptr;char origin[192]{};
    char assets[17][65]{};Entry* bench_pins[17]{};
    unsigned mode=0,rate=30,bench_frame=0,bench_generation=0;int64_t bench_next=0,next_render=0;
#if CONFIG_DG_DUO
    DuoRendered duo_rendered;
#endif
    Character character;CharacterFrame character_frame{99,99,99};
    dg_audio::CharacterSignals::Sample character_audio;
    bool character_enabled=false;int64_t character_next=0;
    unsigned character_changes[3]{},character_posts=0,character_errors=0;
    bool demo_enabled=false;Entry* demo_pins[DemoAssetCount]{};
    DemoAnimation demo_animation;DemoFrame demo_frame;bool demo_started=false;
    unsigned demo_posts=0,demo_errors=0;
    DemoProof demo_proofs[16]{};uint64_t demo_proof_count=0;unsigned demo_last_proof=99;
    bool split=true;std::atomic<bool> failed{false};std::atomic<unsigned> diagnostic{0},measure_frames{0};Trace trace[1024]{};unsigned trace_count=0;
    int64_t compose_us=0,push_us=0;uint64_t stripes=0;unsigned console_sequence=0;std::atomic<uint32_t> te_count{0};uint32_t te_stamps[128]{};
};
extern State* state;
extern std::atomic<DemoScreen> requested_screen;
void meta(Request&,const char*);
void release_bench();
void release_demo();
void start_character();
bool demo_tick(int64_t);
void character_tick(int64_t);
void demo_rendered(const RenderLease&,int64_t);
void dead_display(unsigned reason);
void metric(FlashMetric& m,uint64_t elapsed);
void census(const char* object,unsigned before,esp_err_t result=ESP_OK);
bool await_dma();
bool drain_dma();
bool send(Rect r,uint8_t* bytes);
void init_te();
int te_gpio() noexcept;
}}
