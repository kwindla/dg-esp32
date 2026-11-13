#include "dg_audio/aec_config.h"
#include "dg_display/runtime.h"
#include "performance.h"
#include "sdkconfig.h"
#if CONFIG_DG_CACHE_COUNTERS
#include "cache_metrics.h"
#endif
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_memory_utils.h"
#include "esp_wifi.h"
#include "dg_diagnostics.h"
#include "dg_console.h"
#include "dg_board/power.h"
#include "dg_board/board.h"
#include "dg_audio/aec.h"
#include "dg_audio/mic.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pipecat/esp_peer/stat_json.h"
#include <array>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <mutex>
#include <atomic>
#include <cstdio>
#include "esp_attr.h"
size_t measured_task_stack(TaskHandle_t);
namespace {
using PerfJson=pipecat::esp_peer::StatJson;
struct Last {unsigned id;uint32_t runtime;int64_t time;};
std::array<Last,64> previous{};
size_t previous_count=0;
std::array<size_t,3> largest_min{SIZE_MAX,SIZE_MAX,SIZE_MAX};
std::mutex heap_window_mutex;
std::array<size_t,3> session_window_min{SIZE_MAX,SIZE_MAX,SIZE_MAX};
unsigned session_window_samples=0;
std::array<size_t,3> window_min{SIZE_MAX,SIZE_MAX,SIZE_MAX};
std::atomic<bool> compare_sampling{false};
std::array<size_t,3> shadow_min{SIZE_MAX,SIZE_MAX,SIZE_MAX};
unsigned shadow_samples=0;
unsigned window_samples=0;
int64_t window_start_us=0;
int64_t next_largest_sample_us=0;
constexpr std::array<unsigned,3> heap_caps{{MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT,
    MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT,MALLOC_CAP_DMA|MALLOC_CAP_INTERNAL}};
DRAM_ATTR std::atomic<uint32_t> failed_internal{0},failed_dma{0},failed_psram{0},failed_total{0};
DRAM_ATTR std::atomic<uint64_t> failed_last{0};
void IRAM_ATTR failed_alloc(size_t bytes,uint32_t caps,const char*) {
    // Internal 32-bit counters take the IDF hardware path. The coherent 64-bit pair
    // uses IDF atomics implemented with portENTER/EXIT_CRITICAL_SAFE (ISR safe).
    // No logging, allocation or task mutex in any context.
    failed_total.fetch_add(1,std::memory_order_relaxed);
    if(caps&(MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA) || !(caps&MALLOC_CAP_SPIRAM))failed_internal.fetch_add(1,std::memory_order_relaxed);
    if(caps&MALLOC_CAP_DMA)failed_dma.fetch_add(1,std::memory_order_relaxed);
    if(caps&MALLOC_CAP_SPIRAM)failed_psram.fetch_add(1,std::memory_order_relaxed);
    failed_last.store((uint64_t(bytes)<<32)|caps,std::memory_order_relaxed);
}
unsigned cert_us=0;
size_t stack_size(const std::string& name) {
    if(name=="peer-loop")return CONFIG_DG_PEER_OWNER_STACK_BYTES;
    if(name=="audio-tx")return CONFIG_DG_AUDIO_TX_STACK_BYTES;
    if(name=="peer-rx")return CONFIG_DG_PEER_RX_STACK_BYTES;
    if(name=="audio-playback")return CONFIG_DG_PLAYBACK_STACK_BYTES;
    if(name=="audio-capture")return 16384;
    if(name=="app-control")return 16384;
    if(name=="app-session" || name=="demo-boot")return 16384;
    if(name=="demo-config")return 4096;
    if(name=="power-gauge")return 3072;
    if(name=="wifi-observe")return DG_WIFI_OBSERVE_STACK_BYTES;
    if(name=="pipecat")return 12288;
    if(name=="tone-source")return 8192;
    if(name=="console")return 18432;
    if(name=="diagnostic-log" || name=="console-output")return 4096;
    if(name=="console-uart-rx")return 3072;
    if(name=="main")return CONFIG_ESP_MAIN_TASK_STACK_SIZE;
    if(name=="esp_timer")return CONFIG_ESP_TIMER_TASK_STACK_SIZE;
    if(name=="sys_evt")return CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE;
    if(name=="tcpip")return CONFIG_LWIP_TCPIP_TASK_STACK_SIZE;
    if(name=="ipc0" || name=="ipc1")return CONFIG_ESP_IPC_TASK_STACK_SIZE;
    if(name=="IDLE0" || name=="IDLE1")return configMINIMAL_STACK_SIZE;
    // WiFi and idle are native tasks; their allocation sizes are not public.
    return 0;
}
}
void performance_init() {ESP_ERROR_CHECK(heap_caps_register_failed_alloc_callback(failed_alloc));}
void performance_cert_duration(unsigned us) {cert_us=us;}
// Called only from the owner loop, never from an ISR or audio worker. The short mutex
// makes sampling and PERF reset one transaction across all three capabilities.
void performance_compare_sampling(bool enabled) {compare_sampling=enabled;}
extern "C" void dg_heap_shadow_sample(void) {
    if(!compare_sampling.load(std::memory_order_relaxed))return;
    std::lock_guard<std::mutex> lock(heap_window_mutex);
    for(size_t i=0;i<heap_caps.size();++i)
        shadow_min[i]=std::min(shadow_min[i],heap_caps_get_free_size(heap_caps[i]));
    ++shadow_samples;
}
extern "C" void dg_heap_window_sample(bool media_ready) {
    std::lock_guard<std::mutex> lock(heap_window_mutex);
    for(size_t i=0;i<heap_caps.size();++i) {
        auto free=heap_caps_get_free_size(heap_caps[i]);
        window_min[i]=std::min(window_min[i],free);
        if(media_ready)session_window_min[i]=std::min(session_window_min[i],free);

    }
    const auto now=esp_timer_get_time();
    if(now>=next_largest_sample_us) {
        // Largest-block census walks the heaps. Only the owner does this, at 1 Hz.
        for(size_t i=0;i<2;++i)
            largest_min[i]=std::min(largest_min[i],heap_caps_get_largest_free_block(heap_caps[i]));
        // Preserve the descriptor's alias where both columns describe one heap.
        if(dg_board::info().internal_8bit_dma_alias)largest_min[2]=largest_min[0];
        else largest_min[2]=std::min(largest_min[2],heap_caps_get_largest_free_block(heap_caps[2]));
        next_largest_sample_us=now+1000000;
    }
    ++window_samples;if(media_ready)++session_window_samples;
    if(compare_sampling) {
        for(size_t i=0;i<heap_caps.size();++i)shadow_min[i]=std::min(shadow_min[i],window_min[i]);
        ++shadow_samples;
    }
}
std::string power_status_json() {
    const auto p=dg_board::power::read();
    PerfJson result={{"available",int(dg_board::info().has_battery)},{"valid",p.valid},{"error",p.error},{"samples",p.samples},{"errors",p.errors},
        {"sampled_us",p.sampled_us},{"age_ms",p.sampled_us?(esp_timer_get_time()-p.sampled_us)/1000:-1},
        {"voltage_mv",p.valid?PerfJson(p.voltage_mv):PerfJson(nullptr)},
        {"current_ma",p.valid?PerfJson(p.current_ma):PerfJson(nullptr)},
        {"soc_percent",p.valid?PerfJson(p.soc_percent):PerfJson(nullptr)},
        {"temperature_c",p.valid?PerfJson(p.temperature_centideg_c/100.0):PerfJson(nullptr)}};
    return pipecat::esp_peer::stat_dump(result);
}
namespace {
PerfJson wifi_observation_info(const dg_wifi_observation_status_t& s) {
    PerfJson j={{"schema",1},{"enabled",s.enabled},{"startup_error",s.startup_error},
        {"samples",s.samples},{"retained",s.retained},{"overwritten",s.overwritten},
        {"first_sequence",s.first_sequence},{"last_sequence",s.last_sequence},{"capacity",s.capacity},{"record_bytes",s.record_bytes},
        {"tx_done_registered",s.tx_done_registered},{"tx_done_available",s.tx_done_registered && s.startup_error==ESP_OK},{"tx_retries",nullptr},
        {"send_to_air_us",nullptr},{"channel_occupancy",nullptr},
        {"basis","Buffered <=1Hz API readbacks and native STA TX completion deltas; not RF timestamps, retries, RTP counts or ACK latency"}};
    if(s.enabled)j["configured"]={{"tx_buffer_type",s.tx_buffer_type},{"static_tx_buffers",s.static_tx_buffers},
        {"cache_tx_buffers",s.cache_tx_buffers},{"static_rx_buffers",s.static_rx_buffers},
        {"dynamic_rx_buffers",s.dynamic_rx_buffers},{"ampdu_tx",s.ampdu_tx},{"ampdu_rx",s.ampdu_rx},
        {"tx_ba_window",s.tx_ba_window},{"rx_ba_window",s.rx_ba_window},{"wifi_core",s.wifi_core}};
    return j;
}
}
std::string wifi_observation_status_json(const dg_wifi_observation_status_t& s) {
    return pipecat::esp_peer::stat_dump(wifi_observation_info(s));
}
std::string wifi_observation_row_json(const dg_wifi_observation_t& s) {
    char bssid[18];std::snprintf(bssid,sizeof bssid,"%02x:%02x:%02x:%02x:%02x:%02x",
        s.bssid[0],s.bssid[1],s.bssid[2],s.bssid[3],s.bssid[4],s.bssid[5]);
    PerfJson j={{"sequence",s.sequence},{"counter_begin_us",s.counter_begin_us},
        {"sample_begin_us",s.begin_us},{"sample_end_us",s.end_us},
        {"ps_error",s.ps_error},{"ps_mode",s.ps_error==ESP_OK?PerfJson(s.ps_mode):PerfJson(nullptr)},
        {"ap_error",s.ap_error},{"rssi_dbm",s.ap_error==ESP_OK?PerfJson(s.rssi_dbm):PerfJson(nullptr)},
        {"primary_channel",s.ap_error==ESP_OK?PerfJson(s.primary_channel):PerfJson(nullptr)},
        {"secondary_channel",s.ap_error==ESP_OK?PerfJson(s.secondary_channel):PerfJson(nullptr)},
        {"bssid",s.ap_error==ESP_OK?PerfJson(bssid):PerfJson(nullptr)},
        {"tx_done_success",s.tx_done_success},{"tx_done_failed",s.tx_done_failed}};
    return pipecat::esp_peer::stat_dump(j);
}
std::string performance_snapshot(std::string transport) {
    dg_display::RuntimePhaseScope display_phase(dg_display::RuntimePhase::Perf);
    auto result=PerfJson::parse(transport);
    // Parsing owns its nodes in the fixed stat arena. Release the 32 KiB
    // transport string before the heap census and the output serialization.
    // The console passes a temporary, so taking it by value moves its buffer.
    std::string().swap(transport);
    result["device_us"]=esp_timer_get_time();
    result["power"]=PerfJson::parse(power_status_json());
    const auto display=dg_display::runtime_health();
    result["display"]={{"rendered",display.rendered},{"display_recovery_attempts",display.recovery_attempts},
        {"display_recoveries",display.recoveries},{"gate_timeouts",display.gate_timeouts},
        {"terminal_reason",display.terminal_reason},{"scheduler_delays",display.scheduler_delays},
        {"lock_timeouts",display.lock_timeouts},{"lock_failures",display.lock_failures},{"engine_snapshot",display.engine_snapshot},{"display_gate_timeouts",display.display_gate_timeouts},{"dead",display.dead},
        {"failed",display.failed},{"animation",display.animation},{"dma_errors",display.dma_errors},
        {"send_failures",display.send_failures},{"late_callbacks",display.late_callbacks},
        {"lcd_initialized",display.lcd_initialized},{"lcd_tx_captured",display.crypto.lcd_tx_captured},
        {"priority_applies",display.crypto.priority_applies},{"last_priority_written",display.crypto.last_priority_written},{"tx_pri",display.crypto.tx_pri}};
    result["crypto_dma"]={{"sha_psram_input_bytes",display.crypto.sha_psram_input},
        {"sha_internal_input_bytes",display.crypto.sha_internal_input},
        {"aes_psram_input_bytes",display.crypto.aes_psram_input},
        {"aes_internal_input_bytes",display.crypto.aes_internal_input},
        {"psram_output_capacity_bytes",display.crypto.psram_output_capacity},
        {"internal_output_capacity_bytes",display.crypto.internal_output_capacity},
        {"coverage_lost",display.crypto.coverage_lost},{"gate_failures",display.crypto.gate_failures}};
#if CONFIG_DG_CACHE_COUNTERS
    result["cache_counters"]=cache_metrics_snapshot();
#endif
    auto connect=dg_connection_timings();
    result["connect_timings"]["start_request_us"]=connect.start_us;
    result["connect_timings"]["bot_ready_us"]=connect.ready_us;
    result["connect_timings"]["start_to_ready_us"]=connect.ready_us>=connect.start_us?connect.ready_us-connect.start_us:0;
    result["connect_timings"]["cert_generate_us"]=cert_us;
    // Driver counters exist on AEC-off too. Pipeline send drops are not a
    // DMA overflow witness; use the microphone/driver counters directly.
    // Keep the historical aec.capture object below unchanged for frozen readers.
    const auto capture=dg_audio::microphone_status();
    result["capture"]={{"rx_overflows",capture.rx_overflows},{"rx_completions",capture.rx_completions},
        {"read_errors",capture.read_errors},{"read_timeouts",capture.read_timeouts},{"read_frames",capture.read_frames}};
#if CONFIG_DG_ECHOEAR_AEC
    const auto aec=dg_audio::aec_status();
    result["aec"]={{"enabled",aec.enabled},{"implementation","esp-sr-2.4.7/AFE_AEC"},
        {"qualified",false},{"ns_enabled",false},{"agc_enabled",false},
        {"chunk_samples",aec.chunk_samples},{"adapter_delay_samples",aec.adapter_delay_samples},
        {"internal_bytes",aec.internal_bytes},{"psram_bytes",aec.psram_bytes},
        {"calls",aec.calls},{"wall_us",aec.wall_us},{"max_us",aec.max_us},
        {"input_samples",aec.input_samples},{"output_samples",aec.output_samples},
        {"errors",aec.errors},{"reference_clips",aec.reference_clips},{"output_clips",aec.output_clips},
        {"duration_bucket_upper_us",{1000,2000,4000,8000,12000,16000,24000,nullptr}},
        {"duration_buckets",aec.duration_buckets},
        {"timing_rows",aec.timing_rows},{"timing_row_us",aec.timing_row_us},{"timing_row_max_us",aec.timing_row_max_us}};
    const auto mic=dg_audio::microphone_status();
    result["aec"]["capture"]={{"adc_clips",mic.adc_clips},{"gain_clips",mic.gain_clips},
        {"read_errors",mic.read_errors},{"read_timeouts",mic.read_timeouts},{"read_frames",mic.read_frames},
        {"rx_completions",mic.rx_completions},{"rx_overflows",mic.rx_overflows},
        {"dma_frames",CONFIG_DG_SPEAKER_DMA_FRAMES},
        {"maximum_residue_frames",(CONFIG_DG_SPEAKER_DMA_DESCRIPTORS+1)*CONFIG_DG_SPEAKER_DMA_FRAMES}};
    result["aec"]["reference_health"]={{"low_samples",aec.reference_low_samples},
        {"low_windows",aec.reference_low_windows},{"longest_samples",aec.reference_low_longest_samples},
        {"gain_db",aec.reference_gain_db},{"reference_ratio_ppm_at_0db",aec.reference_ratio_ppm_at_0db},{"playback_power_above",aec.playback_power_threshold},
        {"window_samples",aec.reference_window_samples},
        {"basis","Gap-free blocks audible at both capture bounds; MIC3 power relative to accepted playback and analog gain; diagnostic, not DAC proof or fallback"}};
#endif
    const auto census_free_before=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    const auto census_min_before=heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    std::vector<TaskStatus_t> tasks(uxTaskGetNumberOfTasks()+4);
    const auto census_free_allocated=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    auto count=uxTaskGetSystemState(tasks.data(),tasks.size(),nullptr);
    const auto census_min_collected=heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    result["task_census"]={{"bytes",tasks.size()*sizeof(TaskStatus_t)},
        {"external",esp_ptr_external_ram(tasks.data())},{"internal_before",census_free_before},
        {"internal_allocated",census_free_allocated},{"minimum_before",census_min_before},
        {"minimum_collected",census_min_collected}};
    int64_t now=esp_timer_get_time();std::array<Last,64> next{};size_t next_count=0;
    result["tasks"]=PerfJson::array();result["per_core_idle_percent"]={nullptr,nullptr};
    for(unsigned i=0;i<count;++i) {
        auto& t=tasks[i];unsigned id=t.xTaskNumber;std::string name=t.pcTaskName;
        PerfJson row={{"name",name},{"id",id},{"core",xTaskGetCoreID(t.xHandle)},
            {"priority",t.uxCurrentPriority},{"runtime_us",t.ulRunTimeCounter},
            {"stack_free_bytes",t.usStackHighWaterMark},{"cpu_percent",nullptr}};
        if(xTaskGetCoreID(t.xHandle)==tskNO_AFFINITY)row["core"]="any";
        size_t size=measured_task_stack(t.xHandle);
        if(!size)size=stack_size(name);
        row["stack_bytes"]=size? PerfJson(size):PerfJson(nullptr);
        row["stack_margin_percent"]=size?PerfJson(100.0*t.usStackHighWaterMark/size):PerfJson(nullptr);
        auto end=previous.begin()+previous_count;
        auto prior=std::find_if(previous.begin(),end,[id](const Last& p){return p.id==id;});
        if(prior!=end && now>prior->time) {
            double cpu=100.0*uint32_t(t.ulRunTimeCounter-prior->runtime)/(now-prior->time);
            row["cpu_percent"]=cpu;row["window_us"]=now-prior->time;
            if(name=="IDLE0")result["per_core_idle_percent"][0]=cpu;
            if(name=="IDLE1")result["per_core_idle_percent"][1]=cpu;
        }
        if(next_count<next.size())next[next_count++]={id,uint32_t(t.ulRunTimeCounter),now};
        result["tasks"].push_back(std::move(row));
    }
    result["task_census"]["minimum_after_task_rows"]=heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    previous=next;previous_count=next_count;
    result["cpu_history_overflow"]=count>next.size();
    const auto last_failure=failed_last.load(std::memory_order_relaxed);
    result["failed_allocations"]={{"internal",failed_internal.load()},{"dma",failed_dma.load()},
        {"psram",failed_psram.load()},{"total",failed_total.load()},
        {"last_size",uint32_t(last_failure>>32)},{"last_caps",uint32_t(last_failure)},
        {"basis","Requested bits; generic and DMA failures conservatively count internal, including a benign first failure in a future internal-then-PSRAM fallback; counts overlap; last size/caps one coherent atomic pair"}};
    result["diagnostic_log_drops"]=dg_deferred_log_drops();
    const auto console=dg_console::stats();
    result["console_output"]={{"queued",console.queued},{"disconnected",console.disconnected},
        {"full",console.full},{"transport_drops",console.transport_drops}};
    if(dg_board::info().console==dg_board::ConsoleKind::Uart){
        result["console_output"]["drops"]=console.full+console.transport_drops;
        result["console_output"]["ack_pending"]=console.ack_pending;
        result["console_output"]["rx"]={{"fifo_overflows",console.rx.fifo_overflows},{"buffer_full",console.rx.buffer_full},
            {"frame_errors",console.rx.frame_errors},{"parity_errors",console.rx.parity_errors}};
    }
    std::array<size_t,3> minima,session_minima,largest_minima;
    {
        std::lock_guard<std::mutex> lock(heap_window_mutex);
        auto end=esp_timer_get_time();
        result["heap_window"]={{"schema",1},{"start_us",window_start_us},
            {"end_us",end},{"samples",window_samples},{"session_samples",session_window_samples},
            {"basis","Owner-loop-sampled free minima since previous PERF read; not an allocation hook"}};
        if(shadow_samples) {
            result["heap_sampling_comparison"]={{"owner_samples",window_samples},{"all_loop_samples",shadow_samples},
                {"owner_minima",window_min},{"all_loop_minima",shadow_min},
                {"order",{"internal","psram","dma"}}};
            shadow_min.fill(SIZE_MAX);shadow_samples=0;
        }
        minima=window_min;session_minima=session_window_min;session_window_min.fill(SIZE_MAX);session_window_samples=0;largest_minima=largest_min;window_min.fill(SIZE_MAX);window_samples=0;window_start_us=end;
    }
    unsigned heap_index=0;
    for(auto caps : {std::pair<const char*,unsigned>{"internal",MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT},
                     {"psram",MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT},{"dma",MALLOC_CAP_DMA|MALLOC_CAP_INTERNAL}}) {
        auto largest=heap_index==2?result["heap"]["internal"]["largest"].get<size_t>():heap_caps_get_largest_free_block(caps.second);
        auto index=heap_index++;
        auto minimum=largest_minima[index];
        result["heap"][caps.first]={{"free",heap_caps_get_free_size(caps.second)},
            {"minimum_free",heap_caps_get_minimum_free_size(caps.second)},
            {"largest",largest},{"sampled_largest_minimum",minimum==SIZE_MAX?PerfJson(nullptr):PerfJson(minimum)},
            {"window_minimum_free",minima[index]==SIZE_MAX?PerfJson(nullptr):PerfJson(minima[index])},
            {"window_session_minimum_free",session_minima[index]==SIZE_MAX?PerfJson(nullptr):PerfJson(session_minima[index])}};
    }
    wifi_ap_record_t ap{};auto wifi=esp_wifi_sta_get_ap_info(&ap);
    result["wifi"]={{"rssi_dbm",wifi==ESP_OK?PerfJson(ap.rssi):PerfJson(nullptr)},
        {"channel",wifi==ESP_OK?PerfJson(ap.primary):PerfJson(nullptr)},
        {"retry_rx_stats_available",nullptr},{"statistics_dump_api_available",true},
        {"tx_rx_rate_mbps",nullptr},
        {"phy_11n_capable",wifi==ESP_OK?PerfJson(bool(ap.phy_11n)):PerfJson(nullptr)},
        {"stats_format","wifi-stats console dump; counter availability determined from retained output; negotiated PHY rate unavailable"}};
    result["wifi"]["observation"]=wifi_observation_info(dg_wifi_observe_status());
    result["cpu_basis"]="Runtime deltas / wall time, percent of one core; first observation and new tasks are null";
    return pipecat::esp_peer::stat_dump(result);
}
