#pragma once
#include "engine.h"
#include "present.h"
#include "demo_screen.h"
#include "r4_crypto.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
namespace dg_display {
void runtime_init(esp_lcd_panel_handle_t panel,esp_lcd_panel_io_handle_t io,uint8_t* staging,size_t bytes);
void runtime_display(void*);
void runtime_store(void*);
void runtime_wait_ready();
// Single display-owner task only; nullptr for all other tasks and before init.
Presenter* runtime_presenter() noexcept;
// Bounded asynchronous hand-off to runtime_display. The callback runs between
// frames without the engine mutex, may set mode/submit through Presenter, and
// must not block or re-enter the backend from a Released/scan callback. Keep
// context/buffers alive until fn returns; false leaves them with the sender.
struct DisplayOwnerCommand {void (*fn)(void*,Presenter&)noexcept=nullptr;void* context=nullptr;};
bool runtime_dispatch_owner(DisplayOwnerCommand)noexcept;
PostResult post(const Request& request) noexcept;
// App control-thread hook; bounded admission only, all IO stays on store/display workers.
struct WireReply { char id[33]{}; uint64_t epoch=0,sequence=0; bool accepted=false,duplicate=false,parse_failed=false; Error error=Error::None; unsigned wait_us=0; };
bool server_message(const char* json,bool truncated,WireReply& reply);
enum class WireAckOutcome {Sent, QueueFull, SendFailed, Stale};
void wire_ack_outcome(WireAckOutcome outcome) noexcept;
bool wire_ready(uint64_t& epoch,uint64_t& next_sequence) noexcept;
void command(const char* arguments,bool idle,void (*output)(const char*));
void session_start() noexcept;
void session_down() noexcept;
void session_end() noexcept;
void runtime_diagnostic(unsigned frames);
bool diagnostic_pending() noexcept;
// Platform DMA observation, asserted by the single owner after every transfer.
unsigned dma_bounces() noexcept;
unsigned dma_errors() noexcept;
unsigned dma_generation() noexcept;
void dma_capture_device(bool capture) noexcept;
bool runtime_color_done() noexcept;
void runtime_dma_error(unsigned flags) noexcept;
void runtime_dma_dead() noexcept;
void runtime_dma_start() noexcept;
void runtime_dma_done() noexcept;
struct RuntimeHealth {uint64_t rendered=0;unsigned recovery_attempts=0,recoveries=0,gate_timeouts=0,display_gate_timeouts=0,dma_errors=0,send_failures=0,late_callbacks=0,terminal_reason=0,scheduler_delays=0,lock_timeouts=0,lock_failures=0;bool engine_snapshot=false,dead=false,failed=false,animation=false,lcd_initialized=false;R4CryptoHealth crypto{};};
RuntimeHealth runtime_health() noexcept;
}

namespace dg_display {
enum class RuntimePhase : unsigned { SessionStart, SessionTeardown, Http, Perf, Persistence, Console, Count };
void runtime_phase_begin(RuntimePhase phase) noexcept;
void runtime_phase_end(RuntimePhase phase) noexcept;
struct RuntimePhaseScope {
    RuntimePhase phase;
    explicit RuntimePhaseScope(RuntimePhase p):phase(p){runtime_phase_begin(p);}
    ~RuntimePhaseScope(){runtime_phase_end(phase);}
};
void census_set_clock(unsigned hz);
unsigned lcd_requested_hz() noexcept;
unsigned lcd_actual_khz() noexcept;
uint32_t runtime_spi1_begin(unsigned op,uint32_t address,uint32_t bytes) noexcept;
void runtime_spi1_end(uint32_t token,int error) noexcept;
}

namespace dg_display {
// Boot/provisioning owner only, before any peer. Restore via the existing store
// worker, digest checks and bounded result waits; no fixture-server dependency.
void standalone_assets(const char* const* hashes,size_t count,bool animate=true);
void preload_persisted(const char* hash);
// Configuration task holds the same LCD/flash gate as LittleFS; no live peer.
int idle_configuration_write(int (*write)(void*),void* context);
}
