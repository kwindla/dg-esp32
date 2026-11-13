#include "src/runtime_state.h"
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
#include "dg_display/backend_commands.h"
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
// Buffered whole-record output shares stdout locking with IDF logs.
bool app_console_line(const char* text) noexcept;
namespace dg_display {
namespace detail {
bool flash_begin(unsigned op);void flash_end(unsigned op,int error);
State* state=nullptr;std::atomic<bool> fixed_http_ready{false};
std::atomic<DemoScreen> requested_screen{DemoScreen::Disconnected};
std::atomic<unsigned> phase_counts[unsigned(RuntimePhase::Count)]{};
uint8_t phases(){unsigned mask=0;for(unsigned i=0;i<unsigned(RuntimePhase::Count);++i)if(phase_counts[i].load())mask|=1u<<i;return uint8_t(mask);}
void begin_frame(unsigned kind){state->stripe_context=StripeContext{};state->stripe_context.frame=++state->next_frame;state->stripe_context.kind=uint8_t(kind);}
struct EngineMutex {
    bool terminal()const noexcept{return state->engine_lock_failed.load();}
    bool take(unsigned ms)noexcept{return xSemaphoreTake(state->lock,pdMS_TO_TICKS(ms))==pdTRUE;}
    void give()noexcept{xSemaphoreGive(state->lock);}
    void miss(bool exhausted)noexcept{
        ++state->lock_timeouts;++state->scheduler_delays;
        if(exhausted && !state->engine_lock_failed.exchange(true)){
            ++state->lock_failures;dead_display(5);
            dg_deferred_log("DISPLAY_LOCK_TIMEOUT {\"budget_ms\":%u,\"attempts\":%u,\"terminal_reason\":%u}",EngineLockWaitMs*EngineLockAttempts,EngineLockAttempts,state->terminal_reason.load());
        }
    }
} engine_mutex;
void lock(){if(!acquire_engine_mutex(engine_mutex))throw EngineLockTimeout{};}
void unlock(){engine_mutex.give();}
struct Guard:EngineMutexLease<EngineMutex> {
    explicit Guard(bool throwing=true):EngineMutexLease(engine_mutex){if(!*this && throwing)throw EngineLockTimeout{};}
};
// FreeRTOS entries cannot unwind into the scheduler. Quiesce only this worker;
// terminal health and console diagnostics remain available without the mutex.
void lock_failed_worker()noexcept{for(;;)vTaskDelay(pdMS_TO_TICKS(100));}
void census(const char* object,unsigned before,esp_err_t result){unsigned after=heap_caps_get_free_size(Internal);ESP_LOGI("lcd-census","object=%s before=%u after=%u delta=%d minimum=%u largest=%u psram=%u result=%d",object,before,after,int(before)-int(after),unsigned(heap_caps_get_minimum_free_size(Internal)),unsigned(heap_caps_get_largest_free_block(Internal)),unsigned(heap_caps_get_free_size(Psram)),int(result));}
bool allowed(const char* url,bool any_host){
    if(std::strncmp(url,"http://",7) || std::strchr(url,'@') || std::strchr(url,'#'))return false;
    const char* slash=std::strchr(url+7,'/');size_t authority=slash?size_t(slash-url):std::strlen(url);
    if(authority<=7 || authority>=192)return false;
    size_t colon=authority;for(size_t i=7;i<authority;++i){char c=url[i];if(c==':'){if(colon!=authority)return false;colon=i;}else if(i<colon && !((c>='a' && c<='z') || (c>='A' && c<='Z') || (c>='0' && c<='9') || c=='.' || c=='-'))return false;else if(i>colon && !(c>='0' && c<='9'))return false;}
    if(colon==7)return false;
    if(colon<authority){unsigned port=0;if(colon+1==authority)return false;for(size_t i=colon+1;i<authority;++i){port=port*10+unsigned(url[i]-'0');if(port>65535)return false;}if(!port)return false;}
    auto hex=[](char c){return (c>='0' && c<='9') || (c>='a' && c<='f') || (c>='A' && c<='F');};
    for(size_t i=authority;url[i];++i){unsigned c=static_cast<unsigned char>(url[i]);if(c<33 || c>126 || c==92)return false;if(c=='%'){if(!url[i+1] || !url[i+2] || !hex(url[i+1]) || !hex(url[i+2]))return false;i+=2;}}
    return any_host || (state->origin[0] && std::strlen(state->origin)==authority && !std::strncmp(url,state->origin,authority));
}
bool active(uint64_t order,uint64_t epoch){Guard guard;for(auto& r:state->engine.results)if(r.order==order && r.request.meta.epoch==epoch)return r.state==ResultState::Pending && state->engine.epoch==epoch;return false;}
void finish(uint64_t order,uint64_t epoch,Error error,bool persisted=false){char id[33]{};unsigned kind=0;
    {Guard guard;for(auto& r:state->engine.results)if(r.order==order && r.request.meta.epoch==epoch){std::strcpy(id,r.request.meta.id);kind=unsigned(r.request.kind);state->engine.terminal(r,error,persisted);for(auto& item:state->store_cases)if(item.id[0] && !std::strcmp(item.id,r.request.meta.id)){item.completed=esp_timer_get_time();item.error=error;break;}break;}}
    if(id[0]) {
        char record[256];
        const int length=std::snprintf(record,sizeof(record),"\nDISPLAY_WORK_RESULT {\"id\":\"%s\",\"epoch\":%llu,\"order\":%llu,\"kind\":%u,\"error\":\"%s\",\"persisted\":%s}\n",id,(unsigned long long)epoch,(unsigned long long)order,kind,error_name(error),persisted?"true":"false");
        // Release the display guard before taking the console lock.
        if(length<0 || size_t(length)>=sizeof(record) || !app_console_line(record))
            ESP_LOGE("display-store","Work result console write failed");
    }
}

void publish_flash(){
    size_t capacity=state->fs_capacity,used=state->fs_used;bool available=state->flash.available() && esp_littlefs_info("storage",&capacity,&used)==ESP_OK;
    {Guard guard;state->fs_ready=available;state->fs_capacity=capacity;state->fs_used=used;state->fs_free=available && capacity>=used?capacity-used:0;state->fs_temp=state->flash.temp_bytes;state->fs_count=state->flash.count;state->flash_revision=state->flash.revision;for(unsigned i=0;i<128;++i)state->inventory[i]=state->flash.items[i];}
    size_t admissible=state->flash.max_admissible();{Guard guard;state->fs_admissible=admissible;}
}
Error mount_flash(){
    unsigned before=heap_caps_get_free_size(Internal);esp_vfs_littlefs_conf_t config{};config.base_path="/images";config.partition_label="storage";config.format_if_mount_failed=false;auto result=esp_vfs_littlefs_register(&config);census("littlefs-mount-no-autoformat",before,result);if(result!=ESP_OK)return Error::StorageUnavailable;
    const auto* partition=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_ANY,"storage");if(!partition)return Error::StorageUnavailable;
    before=heap_caps_get_free_size(Internal);auto* scratch=static_cast<uint8_t*>(heap_caps_malloc(524288,Psram));if(!scratch)return Error::RamBudget;Error error=state->flash.mount("/images",partition->size,scratch,524288,true);free(scratch);census("flash-catalog-validate-cleanup",before,error==Error::None?ESP_OK:ESP_FAIL);publish_flash();return error;
}
void persisted_event(Entry& entry,Error error){Guard guard;auto& event=state->persisted[state->persisted_count%32];event=Persisted{};std::strcpy(event.hash,entry.hash);event.error=error;event.number=++state->persisted_count;}
Error persist_entry(Entry& entry){
    RuntimePhaseScope phase(RuntimePhase::Persistence);
    Error error=state->flash.install(entry.hash,entry.bytes,entry.size);publish_flash();
    {Guard guard;if(error==Error::None){entry.persisted=true;entry.flash_known=true;entry.pins[unsigned(Pin::Session)]=0;++state->cache.revision;}else if(!entry.persisted)entry.pins[unsigned(Pin::Session)]=1;}
    persisted_event(entry,error);return error;
}
Error preload(const char* hash,bool session){
    auto* item=state->flash.find(hash);if(!item)return state->flash.available()?Error::NotLoaded:Error::StorageUnavailable;if(!item->valid)return item->error;Entry* target=nullptr;Error error;bool existing=false;
    {Guard guard;error=state->cache.reserve(hash,item->bytes,target);existing=error==Error::None && target->valid;}
    if(error!=Error::None || existing)return error;
    size_t bytes=0;error=state->flash.read(hash,target->bytes,target->size,bytes);Image image;if(error==Error::None && !image.open(target->bytes,bytes))error=Error::InvalidDgi;
    {Guard guard;if(error==Error::None){state->cache.finish(*target,image,session);target->persisted=true;target->flash_known=true;target->pins[unsigned(Pin::Session)]=0;}else state->cache.abort(*target);}
    return error;
}
void flash_checkpoint(unsigned stage,size_t bytes){
    if(state->fault_stage.load()==stage){state->fault_reached=stage;ESP_LOGI("display-flash-fault","stage=%u bytes=%u",stage,unsigned(bytes));while(state->fault_stage.load()==stage)vTaskDelay(1);}
    vTaskDelay(1);
}
void provision_flash(){
    Error error=Error::None;const auto* partition=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_ANY,"storage");if(!partition || !partition->size || partition->size%4096 || flash_mmap_remain())error=Error::StorageUnavailable;
    if(error==Error::None)for(size_t at=0;at<partition->size;at+=4096){if(!flash_begin(0)){error=Error::StorageUnavailable;break;}auto read_error=esp_partition_read(partition,at,state->probe_buffer,4096);flash_end(0,read_error);if(read_error!=ESP_OK){error=Error::StorageUnavailable;break;}bool erased=true;for(auto byte:state->probe_buffer)erased &= byte==255;if(!erased){error=Error::InUse;break;}vTaskDelay(1);}
    unsigned before=heap_caps_get_free_size(Internal);if(error==Error::None && esp_littlefs_format("storage")!=ESP_OK)error=Error::StorageUnavailable;census("littlefs-explicit-format-erased-only",before,error==Error::None?ESP_OK:ESP_FAIL);if(error==Error::None)error=mount_flash();state->provision_error=error;state->provision_done=true;state->provision=false;
}
void fetch(uint64_t order,const Request& q){
    bool permitted;{Guard guard;permitted=allowed(q.url,q.any_host);}if(!permitted){finish(order,q.meta.epoch,Error::Fetch);return;}
    if(auto* known=state->flash.find(q.hash)){if(known->valid){Error error=known->bytes==q.bytes?preload(q.hash,state->session_active.load()):Error::Malformed;finish(order,q.meta.epoch,error,error==Error::None);return;}}
    Entry* target=nullptr;Error error=Error::None;bool session=false,existing=false,already_persisted=false;
    {Guard guard;session=state->engine.session;if(!allowed(q.url,q.any_host))error=Error::Fetch;else {error=state->cache.reserve(q.hash,q.bytes,target);if(error==Error::None && target->valid){existing=true;already_persisted=target->persisted;if(session && !target->persisted)target->pins[unsigned(Pin::Session)]=1;}}}
    if(error!=Error::None){finish(order,q.meta.epoch,error);return;}
    if(existing){finish(order,q.meta.epoch,Error::None,already_persisted);return;}
    RuntimePhaseScope http_phase(RuntimePhase::Http);
    size_t received=0;int64_t began=esp_timer_get_time();auto client=state->http;
    esp_http_client_set_timeout_ms(client,5000);if(esp_http_client_set_url(client,q.url)!=ESP_OK){Guard guard;state->cache.abort(*target);if(auto* r=state->engine.result(q.meta.id))if(r->order==order)state->engine.terminal(*r,Error::Fetch);return;}
    ConnectionScope connection(client,esp_http_client_close);
    unsigned before=heap_caps_get_free_size(Internal);esp_err_t opened=esp_http_client_open(client,0);census("http-open",before,opened);
    if(opened!=ESP_OK)error=Error::Fetch;
    if(error==Error::None){before=heap_caps_get_free_size(Internal);int64_t length=esp_http_client_fetch_headers(client);census("http-headers",before);
        if(esp_http_client_get_status_code(client)!=200 || length!=q.bytes || esp_http_client_is_chunked_response(client))error=Error::Fetch;}
    if(error==Error::None){esp_http_client_set_timeout_ms(client,2000);while(received<q.bytes){
        if(!active(order,q.meta.epoch)){error=Error::Cancelled;break;}
        if(esp_timer_get_time()-began>=30000000){error=Error::Fetch;break;}
        int n=esp_http_client_read(client,reinterpret_cast<char*>(target->bytes+received),std::min(size_t(4096),size_t(q.bytes)-received));if(n<=0){error=Error::Fetch;break;}received+=size_t(n);vTaskDelay(1);
    }
    if(error==Error::None && (!esp_http_client_is_complete_data_received(client) || received!=q.bytes))error=Error::Fetch;}
    connection.close();
    Image decoded;if(error==Error::None){char hash[65];if(!digest(target->bytes,target->size,hash) || std::strcmp(hash,q.hash))error=Error::Hash;else if(!decoded.open(target->bytes,target->size))error=Error::InvalidDgi;}
    if(error==Error::None && !active(order,q.meta.epoch))error=Error::Cancelled;
    bool persisted=false;
    {Guard guard;if(state->engine.epoch!=q.meta.epoch)error=Error::Cancelled;if(error==Error::None){state->cache.finish(*target,decoded,session);persisted=target->persisted;}else state->cache.abort(*target);}
    if(error==Error::None && !state->session_active.load() && !state->defer_persistence.load()){
        {Guard guard;state->cache.pin(*target,Pin::Inflight);}
        Error installed=persist_entry(*target);persisted=installed==Error::None;
        {Guard guard;state->cache.unpin(*target,Pin::Inflight);}
    }
    if(error==Error::None && !persisted){Guard guard;target->pins[unsigned(Pin::Session)]=1;}
    finish(order,q.meta.epoch,error,persisted);
    ESP_LOGI("display-store","load_terminal epoch=%llu order=%llu error=%s bytes=%u duration_us=%lld persisted=%d storage=LittleFS stack_free=%u",(unsigned long long)q.meta.epoch,(unsigned long long)order,error_name(error),unsigned(received),(long long)(esp_timer_get_time()-began),int(persisted),unsigned(uxTaskGetStackHighWaterMark(nullptr)));
}

void metric(FlashMetric& m,uint64_t elapsed){++m.count;m.total+=elapsed;m.maximum=std::max(m.maximum.load(),elapsed);unsigned bin=0;for(auto value=elapsed;value>1 && bin<31;value>>=1)++bin;++m.bins[bin];}
bool flash_begin(unsigned op){
    if(!state)return false;
    configASSERT(xTaskGetCurrentTaskHandle()==state->store_owner);
    if(op>2 || flash_mmap_remain())return false;
    const auto requested=esp_timer_get_time();state->writer_token=false;
    if(!state->gate_bypass){
        state->writer_waiting=true;
        const bool acquired=xSemaphoreTake(state->gate,pdMS_TO_TICKS(2000))==pdTRUE;
        state->writer_waiting=false;
        if(!acquired){++state->gate_timeouts;return false;} // Never flash without a token.
        state->writer_token=true;
    }
    auto count=state->flash_count.load();auto& t=state->flash_trace[count%1024];t.end=0;t.requested=requested;t.start=esp_timer_get_time();t.op=op;t.error=0;state->flash_count.store(count+1,std::memory_order_release);
    metric(state->gate_wait_metrics[op],uint64_t(t.start-requested));
    if(state->display_waiting){++state->ops_while_display_waits;state->max_ops_while_display_waits=std::max(state->max_ops_while_display_waits.load(),state->ops_while_display_waits);}else state->ops_while_display_waits=0;
    return true;
}
void flash_end(unsigned op,int error){
    configASSERT(state && xTaskGetCurrentTaskHandle()==state->store_owner);
    auto& t=state->flash_trace[(state->flash_count.load()-1)%1024];auto end=esp_timer_get_time();t.error=error;t.end=end;
    auto elapsed=uint64_t(end-t.start);metric(state->flash_metrics[op],elapsed);metric(state->gate_hold_metrics[op],elapsed);
    if(state->writer_token){state->writer_token=false;configASSERT(xSemaphoreGive(state->gate)==pdTRUE);}
    // Semaphore give does not reserve ownership. Explicitly let a waiting display
    // run before another block operation can retake the token (bounded progress).
    if(state->display_waiting)vTaskDelay(1);
}
void dead_display(unsigned reason){unsigned clear=0;state->terminal_reason.compare_exchange_strong(clear,reason);state->dead=true;state->failed=true;}
void io_watchdog(void*) {
    auto stripe=state->stripe_start.load();auto now=esp_timer_get_time();
    auto held=state->held_gate_due.load();if(held && now>=held && state->held_gate_due.exchange(0))configASSERT(xSemaphoreGive(state->gate)==pdTRUE);
    auto late=state->late_due.load();if(late && now>=late && state->late_due.exchange(0)){
        state->stripe_completion=now;state->color_inflight=false;++state->late_callbacks;
        if(state->display_token.exchange(false))configASSERT(xSemaphoreGive(state->gate)==pdTRUE);
        if(state->display_owner)xTaskNotifyGive(state->display_owner);
    }
    if(missing_dma_completion(now,stripe,state->dma_started.load(),state->stripe_completion.load(),state->color_inflight.load()))dead_display(1);
}
bool render(RenderLease& lease,uint64_t& pushed,unsigned kind=0){
    bool full=lease.dirty.count==1 && lease.dirty.rectangles[0].x0<=0 && lease.dirty.rectangles[0].y0<=0 && lease.dirty.rectangles[0].x1>=PanelWidth && lease.dirty.rectangles[0].y1>=PanelHeight;
    begin_frame(kind?kind:state->mode==2?5:full?2:1);
    const auto generation=dma_generation();const auto frame_start=esp_timer_get_time();state->frame_gate_wait=0;state->frame_gate_hold=0;
    bool inflight=false;bool ok=true;SubmitId submission;unsigned part=0;size_t capacity=state->split?state->staging_bytes/2:state->staging_bytes;
    for(unsigned i=0;i<lease.dirty.count && ok;++i){Rect r=dma_align(lease.dirty.rectangles[i]);size_t rowbytes=size_t(r.x1-r.x0)*2;int rows=int(capacity/rowbytes);if(DmaYEvenIfRow8 && rowbytes%16)rows&=~1;if(!rows){ok=false;break;}
        for(int y=r.y0;y<r.y1 && ok;y+=rows){
            {Guard guard;if(!state->engine.current(lease)){ok=false;break;}}
            Rect stripe{r.x0,y,r.x1,std::min(int32_t(y+rows),r.y1)};uint8_t* buffer=state->staging+part*capacity;
            if(!state->split && inflight){auto began=esp_timer_get_time();ok=state->presenter.wait_released(submission)==PresentError::None;state->push_us+=esp_timer_get_time()-began;inflight=false;if(!ok)break;}
            auto began=esp_timer_get_time();ok=compose(lease.scene,stripe,buffer,capacity,state->scratch,PanelWidth);state->compose_us+=esp_timer_get_time()-began;if(!ok)break;
            if(inflight){began=esp_timer_get_time();ok=state->presenter.wait_released(submission)==PresentError::None;state->push_us+=esp_timer_get_time()-began;inflight=false;if(!ok)break;}
            // Cancellation can arrive while decoding; recheck BEFORE starting another DMA.
            {Guard guard;if(!state->engine.current(lease)){ok=false;break;}}
            began=esp_timer_get_time();submission=state->presenter.submit_logical(stripe,reinterpret_cast<const uint16_t*>(buffer),size_t(stripe.x1-stripe.x0));ok=bool(submission);state->push_us+=esp_timer_get_time()-began;if(!ok)break;inflight=true;++state->stripes;pushed+=uint64_t(stripe.x1-stripe.x0)*unsigned(stripe.y1-stripe.y0);if(state->split)part^=1;
        }
    }
    if(inflight){auto began=esp_timer_get_time();bool waited=state->presenter.wait_released(submission)==PresentError::None;state->push_us+=esp_timer_get_time()-began;ok=ok && waited;}
    // Draining checks the driver's DMA underflow flag through the wrapper.
    if((submission?state->presenter.committed(submission)!=PresentError::None:!drain_dma()) || dma_generation()!=generation)ok=false;
    Guard trace_guard;
    auto& frame=state->frame_trace[state->frame_count++%128];frame={frame_start,esp_timer_get_time(),state->frame_gate_wait,state->frame_gate_hold.load(),dma_generation(),ok};
    metric(state->gate_wait_metrics[3],uint64_t(frame.wait));metric(state->gate_hold_metrics[3],uint64_t(frame.hold));
    if(state->prior_frame_start)metric(state->frame_gap_metrics,uint64_t(frame_start-state->prior_frame_start));
    state->prior_frame_start=frame_start;
    return ok;
}
void state_represent(){Guard guard;state->engine.dirty.add({0,0,PanelWidth,PanelHeight});}
void meta(Request& q,const char* prefix){q.meta.epoch=state->engine.epoch;q.meta.sequence=state->engine.last_sequence+1;std::snprintf(q.meta.id,33,"%s-%u",prefix,unsigned(q.meta.sequence));}
void release_bench(){for(auto*& e:state->bench_pins)if(e){state->cache.unpin(*e,Pin::Scheduled);e=nullptr;}}
void bench_tick(int64_t now){
    if(!state->mode)return;
    // Account every requested tick even if the owner spent several ticks in DMA.
    unsigned catchup=0;while(now>=state->bench_next && catchup++<64){auto& q=state->bench_request;q.~Request();new(&q)Request{};meta(q,"bench");q.meta.sequence=++state->bench_sequence;std::snprintf(q.meta.id,33,"bench-%llu",(unsigned long long)q.meta.sequence);std::snprintf(q.utterance,33,"bench-%u",state->bench_generation);
        auto show=[&](unsigned layer,unsigned asset,int x,int y){auto& op=q.operations[q.count++];op.kind=Operation::Show;op.layer=layer;op.x=x;op.y=y;std::strcpy(op.hash,state->assets[asset]);};
        unsigned f=state->bench_frame++;
        if(state->mode==2){show(0,state->engine.scene.layers[0].image==&state->bench_pins[0]->handle?16:0,0,0);if(f==0)for(unsigned layer=1;layer<8;++layer){auto& op=q.operations[q.count++];op.kind=Operation::Hide;op.layer=layer;}}
        else {if(f==0 || !state->engine.scene.layers[0].visible)show(0,0,0,0);show(1,1+f%6,cat_coordinate(168),cat_coordinate(192));if(f%5==0){show(2,7+(f/5)%6,cat_coordinate(152),cat_coordinate(176));show(3,13+(f/5)%3,cat_coordinate(92),cat_coordinate(180));}}
        auto admitted=state->engine.post(q,now,true);if(admitted.admission!=Admission::Accepted){state->failed=true;state->mode=0;release_bench();break;}state->bench_next+=1000000/state->rate;
    }
}
void measure_frames(unsigned frames){
    {Guard guard;state->engine.dirty.add({0,0,PanelWidth,PanelHeight});if(!state->engine.snapshot(state->lease)){state->measure_frames=0;return;}}
    uint64_t pushed=0;bool ok=true;
    for(unsigned buffers=1;buffers<=2 && ok;++buffers){state->split=buffers==2;
        for(unsigned patch=0;patch<2 && ok;++patch){int64_t begin_compose=state->compose_us,begin_push=state->push_us,sum=0,min=INT64_MAX,max=0;uint64_t before_pixels=pushed;
            for(unsigned f=0;f<frames && ok;++f){state->lease.dirty.clear();state->lease.dirty.add(patch?Rect{cat_coordinate(92),cat_coordinate(176),cat_coordinate(316),cat_coordinate(232)}:Rect{0,0,PanelWidth,PanelHeight});auto began=esp_timer_get_time();ok=render(state->lease,pushed,4);auto elapsed=esp_timer_get_time()-began;sum+=elapsed;min=std::min(min,elapsed);max=std::max(max,elapsed);}
            ESP_LOGI("display-throughput","buffers=%u rect=%s frames=%u wall_mean_us=%lld wall_min_us=%lld wall_max_us=%lld compose_mean_us=%lld wait_enqueue_mean_us=%lld pixels=%llu bounce=%u dma_errors=%u ok=%d",buffers,patch?"cat-union":"full",frames,(long long)(sum/frames),(long long)min,(long long)max,(long long)((state->compose_us-begin_compose)/frames),(long long)((state->push_us-begin_push)/frames),(unsigned long long)(pushed-before_pixels),dma_bounces(),dma_errors(),int(ok));
        }
    }
    state->split=true;{Guard guard;state->engine.complete(state->lease,ok,pushed);}state->measure_frames=0;
}
void store_bench_tick(int64_t now){
    Guard guard;if(!state->store_bench || state->store_step>=9)return;
    unsigned step=state->store_step;unsigned seconds=step==8?38:2+(step/2)*9+(step%2)*4;if(now-state->store_start<int64_t(seconds)*1000000)return;
    auto& q=state->store_request;q.~Request();new(&q)Request{};meta(q,"5b");std::snprintf(q.meta.id,33,"5b-%u-%u",state->store_generation,step);q.kind=RequestKind::Persist;if(step<8 && step%2)q.kind=RequestKind::Delete;std::strcpy(q.hash,state->store_assets[step==8?4:step/2]);auto& item=state->store_cases[step];std::strcpy(item.id,q.meta.id);item.posted=now;item.kind=q.kind;if(auto* entry=state->cache.find(q.hash))item.bytes=entry->size;auto admitted=state->engine.post(q,now);if(admitted.admission!=Admission::Accepted){item.completed=now;item.error=admitted.error;}++state->store_step;if(state->store_step==9)state->store_bench=false;
}
uint16_t pattern(int x,int y,unsigned frame){if(x<8 || x>=PanelWidth-8 || y<8 || y>=PanelHeight-8)return 0xffff;unsigned index=(y<PanelHeight/2?0:2)+(x<PanelWidth/2?0:1);static constexpr uint16_t colors[4]={0xf800,0x07e0,0x001f,0xffff};return colors[(index+frame)%4];}
void diagnostic_frames(unsigned frames){
    state->trace_count=0;for(unsigned f=0;f<frames;++f){begin_frame(3);const auto generation=dma_generation();auto& t=state->trace[state->trace_count++];t.begin=esp_timer_get_time();t.number=f;t.crc=0;SubmitId submission;for(int y=0;y<PanelHeight;){int rows=std::min(24,PanelHeight-y);size_t at=0;for(int row=y;row<y+rows;++row)for(int x=0;x<PanelWidth;++x){uint16_t c=pattern(x,row,f);state->staging[at++]=c>>8;state->staging[at++]=c;}t.crc=esp_rom_crc32_le(t.crc,state->staging,at);submission=state->presenter.submit_logical({0,y,PanelWidth,y+rows},reinterpret_cast<const uint16_t*>(state->staging),PanelWidth);if(!submission || state->presenter.wait_released(submission)!=PresentError::None){state->failed=true;break;}y+=rows;}if((submission?state->presenter.committed(submission)!=PresentError::None:!drain_dma()) || dma_generation()!=generation)state->failed=true;t.end=esp_timer_get_time();if(state->failed)break;while(esp_timer_get_time()-t.begin<200000)vTaskDelay(1);}
    ESP_LOGI("lcd-qualify","frames=%u failed=%d requested_hz=%u actual_khz=%u staging=20480 bounce=%u dma_errors=%u stack_free=%u",state->trace_count,int(state->failed),lcd_requested_hz(),lcd_actual_khz(),dma_bounces(),dma_errors(),unsigned(uxTaskGetStackHighWaterMark(nullptr)));state->diagnostic=0;
}
void layer_status(void (*out)(const char*)){
    char b[1024];lock();size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_LAYERS {\"epoch\":%llu,\"revision\":%llu,\"layers\":[",(unsigned long long)state->engine.epoch,(unsigned long long)state->engine.scene.revision);
    for(unsigned i=0;i<8;++i){auto& layer=state->engine.scene.layers[i];const char* hash="";if(layer.image){hash="external-raster";for(auto& entry:state->cache.entries)if(entry.valid && &entry.handle==layer.image){hash=entry.hash;break;}}at+=std::snprintf(b+at,sizeof(b)-at,"%s[%u,\"%s\",%d,%d,%u,%u,%s]",i?",":"",i,hash,int(layer.x),int(layer.y),layer.image?layer.image->width:0,layer.image?layer.image->height:0,layer.visible?"true":"false");}
    unlock();if(at+4>=sizeof(b))throw std::runtime_error("layer page exceeds1KB");std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);
}
void status(void (*out)(const char*)){char b[1024];lock();auto& e=state->engine;auto& c=state->cache;int n=std::snprintf(b,sizeof(b),"\nDISPLAY {\"proto\":1,\"caps\":{\"codecs\":[1,2],\"alpha\":[0,1,2],\"ops\":[\"load\",\"preload\",\"show\",\"hide\",\"fill\",\"batch\",\"delete\",\"persist\",\"cancel\"]},\"panel\":[%d,%d,\"rgb565be\",%d],\"dma_y_even_if_row8\":%s,\"epoch\":%llu,\"revisions\":[%llu,%llu,%llu],\"queue\":[%u,%u],\"limits\":[8,16,2,31,524288,512,2000,100],\"clock\":\"fake-only\",\"updates\":[%llu,%llu,%llu,%llu],\"pixels\":%llu,\"deadline_misses\":%llu,\"errors\":[%llu,%llu,%llu],\"ram\":{\"budget\":%u,\"used\":%u,\"reserved\":%u,\"pinned\":%u,\"evictable\":%u,\"free\":%u,\"largest\":%u,\"count\":%u},\"flash\":{\"capacity\":%u,\"used\":%u,\"free\":%u,\"temp_bytes\":%u,\"count\":%u,\"health\":\"%s\",\"policy\":\"outside-session\",\"largest_free\":null,\"largest_free_basis\":\"LittleFS-no-exact-file-metric;free-minus16KiB-reserve\",\"max_admissible_bytes\":%u},\"bench\":[%u,%u,%u],\"dma\":[%u,%u],\"failed\":%s,\"dead\":%s,\"display_recovery_attempts\":%u,\"display_recoveries\":%u,\"gate_timeouts\":%u,\"display_gate_timeouts\":%u,\"diagnostic\":%s,\"send_failures\":%u,\"late_callbacks\":%u}\n",
        PanelWidth,PanelHeight,PanelXAlign,DmaYEvenIfRow8?"true":"false",(unsigned long long)e.epoch,(unsigned long long)e.accepted_revision,(unsigned long long)e.rendering_revision,(unsigned long long)e.completed_revision,e.occupancy(),e.job_count,(unsigned long long)e.requested,(unsigned long long)e.accepted,(unsigned long long)e.rendered,(unsigned long long)e.coalesced,(unsigned long long)e.pixels,(unsigned long long)e.deadline_misses,(unsigned long long)e.errors,(unsigned long long)e.dropped,(unsigned long long)e.superseded,unsigned(c.arena.budget()),unsigned(c.arena.used()),unsigned(c.reserved()),unsigned(c.pinned()),unsigned(c.evictable()),unsigned(c.arena.budget()-c.arena.used()),unsigned(c.arena.largest()),c.count(),unsigned(state->fs_capacity),unsigned(state->fs_used),unsigned(state->fs_free),unsigned(state->fs_temp),state->fs_count,state->fs_ready?"ready":"unavailable",unsigned(state->fs_admissible),state->mode,state->rate,state->bench_frame,dma_bounces(),dma_errors(),state->failed?"true":"false",state->dead?"true":"false",state->display_recovery_attempts,state->display_recoveries,state->gate_timeouts.load(),state->display_gate_timeouts.load(),state->diagnostic_boot?"true":"false",state->send_failures.load(),state->late_callbacks.load());unlock();
    if(n<0 || size_t(n)>=sizeof(b))throw std::runtime_error("status exceeds 1KB");
    out(b);}
}
using namespace detail;
void runtime_dma_start() noexcept{if(state){state->dma_started=esp_timer_get_time();state->dma_inflight=true;r3_stress_dma_start();r5_hash_dma_start();for(unsigned op=0;op<3;++op)if(state->spi_active[op].load())state->stripe_spi_mask.fetch_or(1u<<op);}}
void runtime_dma_done() noexcept{if(state){const auto done=esp_timer_get_time();r3_stress_dma_done(state->dma_started.load(),done);r5_hash_dma_done(state->dma_started.load(),done);state->dma_ended=done;state->dma_inflight=false;++state->stripes_completed;state->bytes_completed.fetch_add(state->stripe_context.bytes);state->dma_active_us.fetch_add(uint64_t(done-state->dma_started.load()));}}
bool runtime_color_done() noexcept{if(state){
    unsigned fault=2;if(state->r2_fault.compare_exchange_strong(fault,0))return false;
    fault=3;if(state->r2_fault.compare_exchange_strong(fault,0)){state->late_due=esp_timer_get_time()+2500000;return false;}
    const auto done=esp_timer_get_time();state->stripe_context.phase=phases();state->stripe_context.spi1=uint8_t(state->stripe_spi_mask.load());
    state->stripe_completion=done;state->color_inflight=false;state->last_color_done=done;
    state->frame_gate_hold.fetch_add(state->stripe_completion.load()-state->stripe_start.load());
    if(state->display_token.exchange(false)){BaseType_t wake=pdFALSE;xSemaphoreGiveFromISR(state->gate,&wake);if(wake)portYIELD_FROM_ISR();}
}return true;}
void runtime_dma_error(unsigned flags) noexcept{if(state){Guard guard(false);if(!guard)return;auto& e=state->dma_error_ring[state->dma_error_count++%ErrorCapacity];e={state->stripe_start.load(),state->stripe_completion.load(),esp_timer_get_time(),flags,{0,0,0},state->stripe_context};
    const auto count=state->flash_count.load(std::memory_order_acquire);unsigned mask=0;
    for(unsigned i=count>1024?count-1024:0;i<count;++i){auto& f=state->flash_trace[i%1024];auto end=f.end.load();if(!end)end=e.recycle;if(f.start<=e.completion && end>=e.start){++e.overlap[f.op];mask|=1u<<f.op;}}
    ++state->error_buckets[mask];
    // Diagnostic durable context: emitted after the consumed transaction, never
    // per copy. Ring rollover cannot erase an already captured error record.
    if(state->diagnostic_boot.load()){std::printf("\nDISPLAY_DMA_ERROR {\"index\":%u,\"record\":[%u,%lld,%lld,%lld,%u,%u,%u,%u,%u,%u,%u,[%u,%u,%u,%u],%u,%u,%u],\"dma_pre_us\":%lld,\"dma_post_us\":%lld,\"spi_api_total\":%u,\"spi_coverage_errors\":%u}\n",state->dma_error_count-1,state->dma_error_count-1,(long long)e.start,(long long)e.completion,(long long)e.recycle,e.flags,e.overlap[0],e.overlap[1],e.overlap[2],e.context.frame,unsigned(e.context.kind),unsigned(e.context.stripe),unsigned(e.context.x0),unsigned(e.context.y0),unsigned(e.context.x1),unsigned(e.context.y1),e.context.bytes,unsigned(e.context.phase),unsigned(e.context.spi1),(long long)state->dma_started.load(),(long long)state->dma_ended.load(),state->spi_count.load(),state->spi_coverage_errors.load());
        r5_hash_error(state->dma_error_count-1);
        const auto count=state->spi_count.load();const auto begin=state->dma_started.load(),end=state->dma_ended.load();
        for(unsigned i=count>SpiCapacity?count-SpiCapacity:0;i<count;++i){auto& t=state->spi_trace[i%SpiCapacity];const auto token=t.token.load(),op=t.op.load();const auto first=t.start.load(),last=t.end.load();
            if(token==i+1 && first<=end && (!last || last>=begin))std::printf("\nDISPLAY_DMA_SPI_ERROR {\"dma_index\":%u,\"record\":[%u,%u,%lld,%lld,%u,%u,%d,%u],\"snapshot_us\":%lld}\n",state->dma_error_count-1,i,op,(long long)first,(long long)last,t.address.load(),t.bytes.load(),t.error.load(),token,(long long)esp_timer_get_time());
        }
    }
}}
void runtime_phase_begin(RuntimePhase phase) noexcept{++phase_counts[unsigned(phase)];}
void runtime_phase_end(RuntimePhase phase) noexcept{--phase_counts[unsigned(phase)];}
uint32_t runtime_spi1_begin(unsigned op,uint32_t address,uint32_t bytes) noexcept{
    if(!state)return 0;
    const auto token=state->spi_count.fetch_add(1)+1;auto& t=state->spi_trace[(token-1)%SpiCapacity];
    t.token=0;t.end=0;t.start=esp_timer_get_time();t.op=op;t.address=address;t.bytes=bytes;t.error=0;t.token.store(token,std::memory_order_release);
    ++state->spi_active[op];if(state->dma_inflight.load())state->stripe_spi_mask.fetch_or(1u<<op);
    return token;
}
void runtime_spi1_end(uint32_t token,int error) noexcept{
    if(!token || !state)return;
    auto& t=state->spi_trace[(token-1)%SpiCapacity];
    if(t.token.load()!=token){++state->spi_coverage_errors;return;}
    auto op=t.op.load();if(state->dma_inflight.load())state->stripe_spi_mask.fetch_or(1u<<op);
    t.error=error;t.end=esp_timer_get_time();--state->spi_active[op];
}
void runtime_dma_dead() noexcept{if(state)dead_display(3);}
bool runtime_crypto_gate_ready() noexcept{return state && state->gate;}
bool runtime_crypto_gate_take() noexcept{if(!state || state->dead)return false;return xSemaphoreTake(state->gate,pdMS_TO_TICKS(2000))==pdTRUE;}
bool runtime_hash_gate_take() noexcept{if(!state || !state->diagnostic_boot || state->dead)return false;return xSemaphoreTake(state->gate,pdMS_TO_TICKS(2000))==pdTRUE;}
void runtime_hash_gate_give() noexcept{configASSERT(state && xSemaphoreGive(state->gate)==pdTRUE);}
RuntimeHealth runtime_health() noexcept{
    RuntimeHealth h;h.dma_errors=dma_errors();if(!state)return h;
    // This may be the acquisition that first discovers a deadlock. Sample the
    // atomic terminal fields afterward, even when no engine snapshot is safe.
    Guard guard(false);
    h.terminal_reason=state->terminal_reason;h.scheduler_delays=state->scheduler_delays;
    h.lock_timeouts=state->lock_timeouts;h.lock_failures=state->lock_failures;
    h.send_failures=state->send_failures;h.late_callbacks=state->late_callbacks;
    h.gate_timeouts=state->gate_timeouts;h.display_gate_timeouts=state->display_gate_timeouts;
    h.dead=state->dead;h.failed=state->failed;h.lcd_initialized=true;h.crypto=r4_crypto_health();
    if(guard){h.engine_snapshot=true;h.rendered=state->engine.rendered;
        h.recovery_attempts=state->display_recovery_attempts;h.recoveries=state->display_recoveries;h.animation=state->mode!=0;}
    return h;
}
void runtime_init(esp_lcd_panel_handle_t panel,esp_lcd_panel_io_handle_t io,uint8_t* staging,size_t bytes){
    // Full staging holds at most StagingBytes/2 source pixels. Scale-two
    // replication needs four times that budget; keep it separate from DMA
    // staging so the adapter cannot overwrite an in-flight/source stripe.
    constexpr size_t native_pixels=dg_board::CanvasScale>1?size_t(StagingBytes/2)*dg_board::CanvasScale*dg_board::CanvasScale:0;
    auto* native_scratch=native_pixels?static_cast<uint16_t*>(heap_caps_aligned_alloc(StagingAlignment,native_pixels*2,Psram)):nullptr;
    if(native_pixels && !native_scratch)throw std::bad_alloc();
    unsigned before=heap_caps_get_free_size(Internal);void* memory=heap_caps_aligned_alloc(16,sizeof(State),Psram);if(!memory)throw std::bad_alloc();state=new(memory) State(native_scratch,native_pixels);const auto* storage=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_ANY,"storage");state->fs_capacity=storage?storage->size:0;state->panel=panel;state->io=io;state->staging=staging;state->staging_bytes=bytes;state->lock=xSemaphoreCreateMutexStatic(&state->lock_storage);state->gate=xSemaphoreCreateBinaryStatic(&state->gate_storage);configASSERT(state->gate);configASSERT(xSemaphoreGive(state->gate)==pdTRUE);if(!state->lock)throw std::bad_alloc();state->clock.count=0;state->engine.epoch=((((uint64_t(esp_random())<<32)|esp_random())&((1ull<<49)-1))+1);state->flash.write_allowed=[](){
#if !CONFIG_SPIRAM_XIP_FROM_PSRAM
        return false; // Diagnostic XIP-off image never executes flash IO on a PSRAM stack.
#else
        return flash_mmap_remain()==0 && (!state->session_active.load() || state->allow_call_persistence.load());
#endif
    };state->flash.free_bytes=[](){return state->fs_free;};census("display-core-psram",before);
    before=heap_caps_get_free_size(Internal);initialize_asset_hash();census("asset-hash-internal-dma-4096",before);
    esp_timer_create_args_t watchdog_args{};watchdog_args.callback=io_watchdog;watchdog_args.name="lcd-watchdog";
    ESP_ERROR_CHECK(esp_timer_create(&watchdog_args,&state->watchdog));ESP_ERROR_CHECK(esp_timer_start_periodic(state->watchdog,10000));
    init_te();
    ESP_LOGI("lcd-census","fixed_state_psram=%u arena=0 storage=LittleFS-explicit-provision",unsigned(sizeof(State)));
}
void runtime_display(void*){
    try{
    state->display_owner=xTaskGetCurrentTaskHandle();
    if(state->adapter.fill_border()!=PresentError::None)throw std::runtime_error("logical border fill");
    for(;;){if(state->failed){vTaskDelay(pdMS_TO_TICKS(100));continue;}if(unsigned count=state->diagnostic.load())diagnostic_frames(count);if(unsigned count=state->measure_frames.load())measure_frames(count);
        panel_backend().poll();
        unsigned pending=2;
        if(state->owner_command_state.compare_exchange_strong(pending,3,std::memory_order_acquire)){
            const auto command=state->owner_command;command.fn(command.context,state->presenter);
            state->owner_command_state.store(0,std::memory_order_release);
        }
        auto now=esp_timer_get_time();bool ready=false;{Guard guard;bench_tick(now);if(demo_tick(now))character_tick(now);state->engine.tick(now);if(now>=state->next_render && state->presenter.mode()==PresentMode::Logical){ready=state->engine.snapshot(state->lease);if(ready)state->next_render=now+33334;}}
        if(ready){uint64_t pixels=0;bool ok=render(state->lease,pixels);{Guard guard;const bool current=state->engine.current(state->lease);state->engine.complete(state->lease,ok,pixels);
#if CONFIG_DG_DUO
            state->duo_rendered.complete(state->lease,ok,current);
#endif
// Future hooks belong at file ends or behind one-line includes; preserve legacy
// configASSERT source-line constants below this existing duo-only hook.
#line 372
            if(ok && current)demo_rendered(state->lease,esp_timer_get_time());
            if(!ok && current && !state->dead){++state->display_recovery_attempts;state->recovery_pending=true;}
            else if(ok && state->recovery_pending){++state->display_recoveries;state->recovery_pending=false;}
        }if(dma_bounces())dead_display(4);}
        vTaskDelay(1);
    }
    }catch(const EngineLockTimeout&){lock_failed_worker();}
}
void runtime_store(void*){
    try{
    state->store_owner=xTaskGetCurrentTaskHandle();
    state->flash.checkpoint=flash_checkpoint;
#if CONFIG_SPIRAM_XIP_FROM_PSRAM
    mount_flash();
#else
    ESP_LOGI("display-store","diagnostic XIP-off: all filesystem IO disabled on PSRAM worker");
#endif
    unsigned before=heap_caps_get_free_size(Internal);esp_http_client_config_t config{};config.url="http://127.0.0.1:7874/";config.timeout_ms=5000;config.disable_auto_redirect=true;config.max_authorization_retries=-1;config.buffer_size=4096;config.buffer_size_tx=1024;state->http=esp_http_client_init(&config);census("http-client-psram-buffers",before,state->http?ESP_OK:ESP_ERR_NO_MEM);fixed_http_ready=true;
    for(;;){
        if(state->metrics_reset.exchange(false)){
            Guard guard;
            auto clear=[](FlashMetric& m){m.count=0;m.total=0;m.maximum=0;for(auto& bin:m.bins)bin=0;};
            for(auto& m:state->flash_metrics){clear(m);}
            for(auto& m:state->gate_wait_metrics){clear(m);}
            for(auto& m:state->gate_hold_metrics){clear(m);}
            clear(state->frame_gap_metrics);
            state->frame_count=0;state->prior_frame_start=0;state->flash_count=0;state->max_stripes_while_writer_waits=0;state->max_ops_while_display_waits=0;state->stripes_while_writer_waits=0;state->ops_while_display_waits=0;state->metrics_reset_done=true;
        }
        store_bench_tick(esp_timer_get_time());
        if(state->provision)provision_flash();
        if(state->metadata_walk.exchange(false)){auto began=esp_timer_get_time();publish_flash();state->flash.max_admissible();ESP_LOGI("display-flash","metadata_walk_us=%lld",(long long)(esp_timer_get_time()-began));}
        if(state->probe_pending){auto client=state->http;size_t total=0;bool ok=false;auto began=esp_timer_get_time();esp_http_client_set_url(client,state->probe_url);esp_http_client_set_timeout_ms(client,5000);unsigned previous=heap_caps_get_free_size(Internal);auto opened=esp_http_client_open(client,0);census("probe-http-open",previous,opened);if(opened==ESP_OK){previous=heap_caps_get_free_size(Internal);int64_t bytes=esp_http_client_fetch_headers(client);census("probe-http-headers",previous);if(bytes>0 && bytes<=524288 && esp_http_client_get_status_code(client)==200 && !esp_http_client_is_chunked_response(client)){esp_http_client_set_timeout_ms(client,2000);while(total<size_t(bytes) && esp_timer_get_time()-began<30000000){int n=esp_http_client_read(client,reinterpret_cast<char*>(state->probe_buffer),std::min(size_t(4096),size_t(bytes)-total));if(n<=0)break;total+=n;vTaskDelay(1);}ok=total==size_t(bytes) && esp_http_client_is_complete_data_received(client);}}esp_http_client_close(client);census("probe-http-complete",heap_caps_get_free_size(Internal));ESP_LOGI("lcd-census","probe_complete ok=%d bytes=%u duration_us=%lld stack_free=%u",int(ok),unsigned(total),(long long)(esp_timer_get_time()-began),unsigned(uxTaskGetStackHighWaterMark(nullptr)));state->probe_pending=false;
        }
        uint64_t order=0;{Guard guard;auto* r=state->engine.next_job();if(r && r->state==ResultState::Pending){state->work=r->request;order=r->order;}}
        if(order){
            auto& q=state->work;if(!active(order,q.meta.epoch))continue;
            if(q.kind==RequestKind::Load){if(state->http)fetch(order,q);else finish(order,q.meta.epoch,Error::Fetch);}
            else if(q.kind==RequestKind::Preload){Error error=preload(q.hash,state->session_active.load());finish(order,q.meta.epoch,error,error==Error::None);}
            else if(q.kind==RequestKind::Persist){
                Entry* target=nullptr;Error error=Error::None;bool durable=false;{Guard guard;target=state->cache.find(q.hash);if(auto* known=state->flash.find(q.hash))durable=known->valid;if(durable){}else if(!target || !target->valid)error=Error::NotLoaded;else if(state->session_active.load() && !state->allow_call_persistence.load())error=Error::StorageUnavailable;else if(!state->cache.pin(*target,Pin::Inflight))error=Error::InUse;}
                if(error==Error::None && !durable){error=persist_entry(*target);{Guard guard;state->cache.unpin(*target,Pin::Inflight);}}finish(order,q.meta.epoch,error,error==Error::None);
            }else if(q.kind==RequestKind::Delete){
                Error error;{Guard guard;auto* target=state->cache.find(q.hash);error=target && target->active_references()?Error::InUse:Error::None;if(error==Error::None && target && target->valid)state->cache.pin(*target,Pin::Inflight);}
                if(error==Error::None){bool physical=state->flash.find(q.hash)!=nullptr;error=physical?state->flash.erase(q.hash):Error::None;{Guard guard;if(auto* target=state->cache.find(q.hash)){state->cache.unpin(*target,Pin::Inflight);if(error==Error::None)error=state->cache.erase(q.hash);}}if(physical)publish_flash();}finish(order,q.meta.epoch,error);
            }
        }else if(!state->session_active.load() && !state->defer_persistence.load() && state->flash.available()){
            // Whole-session pins survive reconnect and are released only after durable install.
            Entry* target=nullptr;{Guard guard;for(auto& entry:state->cache.entries)if(entry.valid && !entry.persisted && entry.pins[unsigned(Pin::Session)] && !entry.pins[unsigned(Pin::Inflight)]){target=&entry;state->cache.pin(entry,Pin::Inflight);break;}}
            if(target){persist_entry(*target);{Guard guard;state->cache.unpin(*target,Pin::Inflight);}vTaskDelay(pdMS_TO_TICKS(1000));}
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    }catch(const EngineLockTimeout&){lock_failed_worker();}
}
void runtime_diagnostic(unsigned frames){if(!state || !frames || frames>1024 || state->mode || state->diagnostic.load() || state->engine.rendering)throw std::runtime_error("invalid qualification request");if(state->dead)throw std::runtime_error("display dead; reboot required");state->failed=false;state->diagnostic=frames;while(state->diagnostic && !state->dead)vTaskDelay(1);if(state->failed)throw std::runtime_error("configured-clock qualification failed");}
bool diagnostic_pending()noexcept{return state && state->diagnostic.load();}
bool physical_write_allowed()noexcept{return state && state->flash.write_allowed && state->flash.write_allowed();}
Presenter* runtime_presenter()noexcept{return state && xTaskGetCurrentTaskHandle()==state->display_owner?&state->presenter:nullptr;}
bool runtime_dispatch_owner(DisplayOwnerCommand command)noexcept{
    if(!state || !command.fn || state->failed || state->dead)return false;
    unsigned empty=0;
    if(!state->owner_command_state.compare_exchange_strong(empty,1,std::memory_order_acquire))return false;
    state->owner_command=command;state->owner_command_state.store(2,std::memory_order_release);return true;
}
void runtime_wait_ready(){int64_t began=esp_timer_get_time();while(!fixed_http_ready){if(esp_timer_get_time()-began>30000000)throw std::runtime_error("store init timeout");vTaskDelay(1);}if(!state->http)throw std::runtime_error("HTTP client allocation failed");}
void demo_screen(DemoScreen screen)noexcept {requested_screen.store(screen);}
void session_start()noexcept {RuntimePhaseScope phase(RuntimePhase::SessionStart);if(!state)return;state->session_active=true;Guard guard(false);if(!guard)return;state->engine.start_session();}
void session_down()noexcept {RuntimePhaseScope phase(RuntimePhase::SessionTeardown);if(!state)return;Guard guard(false);if(!guard)return;state->engine.transport_down();}
void session_end()noexcept {RuntimePhaseScope phase(RuntimePhase::SessionTeardown);if(!state)return;state->session_active=false;Guard guard(false);if(!guard)return;state->engine.end_session();}
PostResult post(const Request& q) noexcept {
    if(!state)return {Admission::Invalid,Error::StorageUnavailable,false};
    if(state->engine_lock_failed)return {Admission::Invalid,Error::StorageUnavailable,false};
    if(xSemaphoreTake(state->lock,0)!=pdTRUE)return {Admission::Full,Error::QueueFull,false};
    if(state->engine_lock_failed){unlock();return {Admission::Invalid,Error::StorageUnavailable,false};}
    auto admitted=state->engine.post(q,esp_timer_get_time());unlock();return admitted;
}
bool wire_ready(uint64_t& epoch,uint64_t& next_sequence) noexcept {
    if(!state)return false;
    Guard guard(false);if(!guard)return false;
    epoch=state->engine.epoch;next_sequence=state->engine.last_sequence+1;return true;
}
void wire_ack_outcome(WireAckOutcome outcome) noexcept {
    if(!state)return;
    switch(outcome){
    case WireAckOutcome::Sent:++state->ack_sent;break;
    case WireAckOutcome::QueueFull:++state->ack_queue_full;break;
    case WireAckOutcome::SendFailed:++state->ack_send_failed;break;
    case WireAckOutcome::Stale:++state->ack_stale;break;
    }
}
bool server_message(const char* json,bool truncated,WireReply& reply) {
    if(truncated)return false;
    Request request;bool matched=false;Meta correlation;
    Error error=parse_server_request(json,std::strlen(json),request,matched,&correlation);
    reply.epoch=correlation.epoch;reply.sequence=correlation.sequence;
    std::strcpy(reply.id,correlation.id);reply.parse_failed=error!=Error::None;
    if(!matched)return false;
    PostResult result{};
    if(state)++state->wire_received;
    if(error==Error::None){
        reply.epoch=request.meta.epoch;reply.sequence=request.meta.sequence;
        if(!state || state->engine_lock_failed)error=Error::StorageUnavailable;
        else {
            auto began=esp_timer_get_time();
            const TickType_t wait=state->wire_wait_ms?pdMS_TO_TICKS(state->wire_wait_ms)+1:0;
            bool locked=xSemaphoreTake(state->lock,wait)==pdTRUE;
            reply.wait_us=unsigned(esp_timer_get_time()-began);
            state->wire_wait_max_us=std::max(state->wire_wait_max_us.load(),reply.wait_us);
            if(!locked){++state->wire_busy;error=Error::QueueFull;}
            else if(state->engine_lock_failed){unlock();error=Error::StorageUnavailable;}
            else {result=state->engine.post(request,esp_timer_get_time());unlock();error=result.error;
                if(result.admission==Admission::Full)++state->wire_queue_full;}
        }
    }
    reply.error=error;reply.accepted=error==Error::None;reply.duplicate=result.duplicate;
    if(reply.accepted){std::strcpy(reply.id,request.meta.id);if(state)++state->wire_accepted;}
    else if(state && error!=Error::QueueFull)++state->wire_invalid;
    dg_deferred_log("DISPLAY_WIRE {\"id\":\"%s\",\"accepted\":%s,\"error\":\"%s\",\"duplicate\":%s,\"wait_us\":%u}",reply.id,reply.accepted?"true":"false",error_name(error),reply.duplicate?"true":"false",reply.wait_us);
    return true;
}
void backend_runtime_command(const char* action,const char* payload,bool idle,void (*out)(const char*)){
    if(!state && std::strcmp(action,"init") && std::strcmp(action,"clock") && std::strcmp(action,"r4-guard") && std::strcmp(action,"r4-crypto"))throw std::runtime_error("disp init first");
    if(!std::strcmp(action,"lock-status")){
        char b[320];std::snprintf(b,sizeof(b),"\nDISPLAY_LOCK_STATUS {\"wait_ms\":%u,\"attempts\":%u,\"timeouts\":%u,\"failures\":%u,\"terminal\":%s,\"terminal_reason\":%u,\"dead\":%s,\"failed\":%s}\n",
            EngineLockWaitMs,EngineLockAttempts,state->lock_timeouts.load(),state->lock_failures.load(),state->engine_lock_failed?"true":"false",state->terminal_reason.load(),state->dead?"true":"false",state->failed?"true":"false");out(b);return;
    }
    if(!std::strcmp(action,"character")){
        const bool on=!std::strcmp(payload,"on");if(!on && std::strcmp(payload,"off"))throw std::runtime_error("character on|off");
        Guard guard;release_demo();state->character_enabled=false;state->mode=0;release_bench();
        if(on)start_character();
        return;
    }
    if(!std::strcmp(action,"demo")){
        const bool on=!std::strcmp(payload,"on");if(!on && std::strcmp(payload,"off"))throw std::runtime_error("demo on|off");
        Guard guard;release_demo();state->character_enabled=false;state->mode=0;release_bench();
        if(on){
            if(!dg_board::info().simple_ui)throw std::runtime_error(dg_board::info().simple_ui_error);
            for(unsigned i=0;i<DemoAssetCount;++i){auto* e=state->cache.find(demo_assets[i].hash);
                if(!e || !e->persisted || !state->cache.pin(*e,Pin::Scheduled)){release_demo();throw std::runtime_error("demo status asset not persisted/preloaded");}state->demo_pins[i]=e;
            }
            try{start_character();}catch(...){release_demo();throw;}
            state->demo_animation=DemoAnimation{};state->demo_started=false;state->demo_posts=state->demo_errors=0;state->demo_last_proof=99;state->demo_enabled=true;
        }return;
    }
    if(!std::strcmp(action,"demo-screen")){
        if(!std::strcmp(payload,"disconnected"))demo_screen(DemoScreen::Disconnected);
        else if(!std::strcmp(payload,"connecting"))demo_screen(DemoScreen::Connecting);
        else if(!std::strcmp(payload,"live"))demo_screen(DemoScreen::Live);
        else throw std::runtime_error("demo-screen disconnected|connecting|live");
        return;
    }
    if(!std::strcmp(action,"demo-proof")){
        unsigned long long start=0;std::sscanf(payload,"%llu",&start);char b[1024];
        {Guard guard;const auto count=state->demo_proof_count;const auto oldest=count>16?count-15:1;start=std::max(start,(unsigned long long)oldest);
            size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_DEMO_PROOF {\"total\":%llu,\"oldest\":%llu,\"items\":[",(unsigned long long)count,(unsigned long long)oldest);unsigned emitted=0;
            for(auto n=start;n<=count && emitted<4;++n){const auto& proof=state->demo_proofs[(n-1)%16];
                at+=std::snprintf(b+at,sizeof(b)-at,"%s{\"n\":%llu,\"screen\":%u,\"completed_us\":%lld,\"revision\":%llu,\"rendered\":%llu}",emitted++?",":"",(unsigned long long)proof.number,proof.screen,(long long)proof.completed,(unsigned long long)proof.revision,(unsigned long long)proof.rendered);
            }
            std::snprintf(b+at,sizeof(b)-at,"]}\n");
        }out(b);return;
    }
    if(!std::strcmp(action,"demo-status")){
        char b[256];{Guard guard;std::snprintf(b,sizeof(b),"\nDISPLAY_DEMO {\"enabled\":%s,\"requested\":%u,\"screen\":%u,\"dots\":%u,\"posts\":%u,\"errors\":%u,\"rendered\":%llu}\n",
            state->demo_enabled?"true":"false",unsigned(requested_screen.load()),unsigned(state->demo_frame.screen),state->demo_frame.dots,state->demo_posts,state->demo_errors,(unsigned long long)state->engine.rendered);}
        out(b);return;
    }
    if(!std::strcmp(action,"character-status")){
        char b[512];
        {Guard guard;std::snprintf(b,sizeof(b),"\nDISPLAY_CHARACTER {\"device_us\":%lld,\"enabled\":%s,\"speaking\":%s,\"attentive\":%s,\"frames\":[%u,%u,%u],\"changes\":[%u,%u,%u],\"blinks\":%u,\"twitches\":%u,\"posts\":%u,\"errors\":%u,\"rendered\":%llu,\"speaker_power\":%u,\"mic_power\":%u}\n",
            (long long)esp_timer_get_time(),state->character_enabled?"true":"false",state->character.speaking()?"true":"false",state->character.attentive()?"true":"false",
            state->character_frame.mouth,state->character_frame.eyes,state->character_frame.whiskers,state->character_changes[0],state->character_changes[1],state->character_changes[2],state->character.blinks(),state->character.twitches(),state->character_posts,state->character_errors,(unsigned long long)state->engine.rendered,unsigned(state->character_audio.speaker_power),unsigned(state->character_audio.mic_power));}
        out(b);return;
    }
    if(!std::strcmp(action,"frame-clock")){
        uint64_t rendered=0;int64_t sampled=0;
        {Guard guard;rendered=state->engine.rendered;sampled=esp_timer_get_time();}
        // Release the compositor mutex before console IO. This endpoint avoids
        // full PERF serialization while measuring an entire live reply.
        char b[128];std::snprintf(b,sizeof(b),"\nDISPLAY_FRAME_CLOCK {\"device_us\":%lld,\"display\":{\"rendered\":%llu}}\n",(long long)sampled,(unsigned long long)rendered);out(b);return;
    }
    if(!std::strcmp(action,"wire-wait")){unsigned ms=0;if(!idle || std::sscanf(payload,"%u",&ms)!=1 || (ms!=0 && ms!=5))throw std::runtime_error("wire-wait Idle 0|5 only");state->wire_wait_ms=ms;return;}
    if(!std::strcmp(action,"wire-status")){char b[512];std::snprintf(b,sizeof(b),"\nDISPLAY_WIRE_STATUS {\"received\":%u,\"accepted\":%u,\"busy\":%u,\"queue_full\":%u,\"invalid\":%u,\"wait_max_us\":%u,\"wait_ms\":%u,\"log_drops\":%u,\"ack_sent\":%u,\"ack_queue_full\":%u,\"ack_send_failed\":%u,\"ack_stale\":%u}\n",state->wire_received.load(),state->wire_accepted.load(),state->wire_busy.load(),state->wire_queue_full.load(),state->wire_invalid.load(),state->wire_wait_max_us.load(),state->wire_wait_ms,dg_deferred_log_drops(),state->ack_sent.load(),state->ack_queue_full.load(),state->ack_send_failed.load(),state->ack_stale.load());out(b);return;}
    if(!std::strcmp(action,"clock")){unsigned hz=0;if(!idle || std::sscanf(payload,"%u",&hz)!=1)throw std::runtime_error("clock requires fresh Idle boot");census_set_clock(hz);return;}
    if(!std::strcmp(action,"r4-guard")){if(!idle)throw std::runtime_error("crypto guard requires Idle");unsigned mode=0;if(std::sscanf(payload,"%u",&mode)!=1)throw std::runtime_error("guard0|1|2");r4_crypto_guard(mode);return;}
    if(!std::strcmp(action,"r4-hash-status")){asset_hash_status(out);return;}
    if(!std::strcmp(action,"r4-crypto")){unsigned start=0;std::sscanf(payload,"%u",&start);r4_crypto_status(start,out);return;}
    if(!std::strcmp(action,"r5-hash-status")){r5_hash_status(out);return;}
    if(!std::strcmp(action,"r5-hash")){if(!state || !state->diagnostic_boot || !idle)throw std::runtime_error("hash stressor requires diagnostic Idle boot");if(!std::strcmp(payload,"off")){r5_hash_stop();return;}unsigned core=0,mode=0;if(std::sscanf(payload,"%u %u",&core,&mode)!=2)throw std::runtime_error("r5-hash core0|1 mode0..4");r5_hash_start(core,mode);return;}
    if(!std::strcmp(action,"r3-stress-status")){r3_stress_status(out);return;}
    if(!std::strcmp(action,"r3-stress")){if(!state->diagnostic_boot || !idle)throw std::runtime_error("stressor requires diagnostic Idle boot");if(!std::strcmp(payload,"off")){r3_stress_stop();return;}unsigned burst=0,period=0,phase=0,core=1;if(std::sscanf(payload,"%u %u %u %u",&burst,&period,&phase,&core)<3)throw std::runtime_error("stress burst_us period_us phase_us [core0|1]|off");r3_stress_start(burst,period,phase,core);return;}
    if(!std::strcmp(action,"r3-info")){char b[768];Guard guard;std::snprintf(b,sizeof(b),"\nDISPLAY_R3_INFO {\"device_us\":%lld,\"requested_hz\":%u,\"actual_khz\":%u,\"rendered\":%llu,\"attempted\":%llu,\"queued\":%llu,\"completed\":%llu,\"attempted_bytes\":%llu,\"completed_bytes\":%llu,\"dma_active_us\":%llu,\"scheduling_gap_max_us\":%llu,\"dma_errors\":%u,\"spi_api_count\":%u,\"spi_coverage_errors\":%u,\"dead\":%s}\n",(long long)esp_timer_get_time(),lcd_requested_hz(),lcd_actual_khz(),(unsigned long long)state->engine.rendered,(unsigned long long)state->stripes_attempted.load(),(unsigned long long)state->stripes_queued.load(),(unsigned long long)state->stripes_completed.load(),(unsigned long long)state->bytes_attempted.load(),(unsigned long long)state->bytes_completed.load(),(unsigned long long)state->dma_active_us.load(),(unsigned long long)state->scheduling_gap_max_us.load(),dma_errors(),state->spi_count.load(),state->spi_coverage_errors.load(),state->dead?"true":"false");out(b);return;}
    if(!std::strcmp(action,"spi-trace")){unsigned start=0;std::sscanf(payload,"%u",&start);char b[1024];auto count=state->spi_count.load();unsigned oldest=count>SpiCapacity?count-SpiCapacity:0;start=std::max(start,oldest);size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_SPI_TRACE {\"total\":%u,\"oldest\":%u,\"start\":%u,\"items\":[",count,oldest,start);for(unsigned i=start;i<count && i<start+6;++i){auto& t=state->spi_trace[i%SpiCapacity];at+=std::snprintf(b+at,sizeof(b)-at,"%s[%u,%u,%lld,%lld,%u,%u,%d,%u]",i==start?"":",",i,t.op.load(),(long long)t.start.load(),(long long)t.end.load(),t.address.load(),t.bytes.load(),t.error.load(),t.token.load());}std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}
    if(!std::strcmp(action,"r2-reset-metrics")){if(!state || !idle)throw std::runtime_error("Idle/init required");{Guard guard;if(state->mode || state->engine.rendering || state->engine.occupancy() || state->engine.job_count || !state->defer_persistence)throw std::runtime_error("metrics reset requires quiet display/store with persistence deferred");}state->metrics_reset_done=false;state->metrics_reset=true;auto began=esp_timer_get_time();while(!state->metrics_reset_done){if(esp_timer_get_time()-began>2000000)throw std::runtime_error("metrics reset timeout");vTaskDelay(1);}return;}
    if(!std::strcmp(action,"r2-fault")){if(!state || !idle || !state->diagnostic_boot || state->dead)throw std::runtime_error("live diagnostic Idle/init required");unsigned fault=!std::strcmp(payload,"prequeue")?1:!std::strcmp(payload,"lost")?2:!std::strcmp(payload,"late")?3:!std::strcmp(payload,"writer-timeout")?4:!std::strcmp(payload,"display-timeout")?5:0;if(!fault || state->r2_fault || state->held_gate_due || state->late_due)throw std::runtime_error("one valid diagnostic fault required");if(fault>=4){if(state->mode || state->color_inflight)throw std::runtime_error("timeout injection requires bench off and drained color");if(xSemaphoreTake(state->gate,pdMS_TO_TICKS(2000))!=pdTRUE)throw std::runtime_error("diagnostic gate acquisition failed");state->held_gate_due=esp_timer_get_time()+3000000;}else state->r2_fault=fault;return;}
    if(!std::strcmp(action,"r2")){if(!state || !idle)throw std::runtime_error("r2 diagnostics require Idle/init");char option[32];unsigned enabled=0;if(std::sscanf(payload,"%31s %u",option,&enabled)!=2 || enabled>1)throw std::runtime_error("r2 option 0|1");if(!std::strcmp(option,"diagnostic")){if(!enabled && state->gate_bypass)throw std::runtime_error("clear bypass first");state->diagnostic_boot=enabled;}else if(!std::strcmp(option,"gate-bypass") && state->diagnostic_boot)state->gate_bypass=enabled;else throw std::runtime_error("diagnostic flag required");return;}
    if(!std::strcmp(action,"frame-trace")){Guard guard;unsigned start=0;std::sscanf(payload,"%u",&start);char b[1024];unsigned count=state->frame_count,oldest=count>128?count-128:0;start=std::max(start,oldest);size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_FRAME_TRACE {\"total\":%u,\"oldest\":%u,\"start\":%u,\"items\":[",count,oldest,start);for(unsigned i=start;i<count && i<start+8;++i){auto& f=state->frame_trace[i%128];at+=std::snprintf(b+at,sizeof(b)-at,"%s[%u,%lld,%lld,%lld,%lld,%u,%s]",i==start?"":",",i,(long long)f.start,(long long)f.end,(long long)f.wait,(long long)f.hold,f.generation,f.success?"true":"false");}std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}
    if(!std::strcmp(action,"gate-stats")){unsigned op=0;if(std::sscanf(payload,"%u",&op)!=1 || op>4)throw std::runtime_error("gate-stats 0=read 1=prog 2=erase 3=frame 4=gap");char b[1400];auto& w=op==4?state->frame_gap_metrics:state->gate_wait_metrics[op];auto& h=op==4?state->frame_gap_metrics:state->gate_hold_metrics[op];size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_GATE_STATS {\"op\":%u,\"progress_max\":[%u,%u],\"wait\":[%llu,%llu,%llu],\"hold\":[%llu,%llu,%llu],\"wait_bins\":[",op,state->max_stripes_while_writer_waits.load(),state->max_ops_while_display_waits.load(),(unsigned long long)w.count.load(),(unsigned long long)w.total.load(),(unsigned long long)w.maximum.load(),(unsigned long long)h.count.load(),(unsigned long long)h.total.load(),(unsigned long long)h.maximum.load());for(unsigned i=0;i<32;++i)at+=std::snprintf(b+at,sizeof(b)-at,"%s%u",i?",":"",w.bins[i].load());at+=std::snprintf(b+at,sizeof(b)-at,"],\"hold_bins\":[");for(unsigned i=0;i<32;++i)at+=std::snprintf(b+at,sizeof(b)-at,"%s%u",i?",":"",h.bins[i].load());std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}
    if(!std::strcmp(action,"flash-trace")){if(!state)throw std::runtime_error("init first");unsigned start=0;std::sscanf(payload,"%u",&start);char b[1024];auto count=state->flash_count.load();unsigned oldest=count>1024?count-1024:0;start=std::max(start,oldest);size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_FLASH_TRACE {\"total\":%u,\"oldest\":%u,\"start\":%u,\"items\":[",count,oldest,start);for(unsigned i=start;i<count && i<start+8;++i){auto& t=state->flash_trace[i%1024];at+=std::snprintf(b+at,sizeof(b)-at,"%s[%u,%u,%lld,%lld,%lld,%d]",i==start?"":",",i,t.op.load(),(long long)t.requested.load(),(long long)t.start.load(),(long long)t.end.load(),t.error.load());}std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}
    if(!std::strcmp(action,"flash-stats")){if(!state)throw std::runtime_error("init first");char b[1024];size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_FLASH_STATS {\"ops\":[");for(unsigned op=0;op<3;++op){auto& m=state->flash_metrics[op];at+=std::snprintf(b+at,sizeof(b)-at,"%s[%llu,%llu,%llu]",op?",":"",(unsigned long long)m.count.load(),(unsigned long long)m.total.load(),(unsigned long long)m.maximum.load());}at+=std::snprintf(b+at,sizeof(b)-at,"],\"error_overlap_masks\":[");for(unsigned i=0;i<8;++i)at+=std::snprintf(b+at,sizeof(b)-at,"%s%u",i?",":"",state->error_buckets[i]);std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}
    if(!std::strcmp(action,"metadata-walk")){if(!state || !idle)throw std::runtime_error("Idle/init required");state->metadata_walk=true;return;}
    if(!std::strcmp(action,"ram-evict")){if(!state || !idle)throw std::runtime_error("Idle/init required");Guard guard;auto error=state->cache.erase(payload);if(error!=Error::None)throw std::runtime_error(error_name(error));return;}
    if(!std::strcmp(action,"dma-trace")){unsigned start=0;std::sscanf(payload,"%u",&start);char b[1024];Guard guard;unsigned oldest=state->dma_error_count>ErrorCapacity?state->dma_error_count-ErrorCapacity:0;start=std::max(start,oldest);size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_DMA_TRACE {\"total\":%u,\"oldest\":%u,\"start\":%u,\"items\":[",state->dma_error_count,oldest,start);for(unsigned i=start;i<state->dma_error_count && i<start+3;++i){auto& e=state->dma_error_ring[i%ErrorCapacity];auto& c=e.context;at+=std::snprintf(b+at,sizeof(b)-at,"%s[%u,%lld,%lld,%lld,%u,%u,%u,%u,%u,%u,%u,[%u,%u,%u,%u],%u,%u,%u]",i==start?"":",",i,(long long)e.start,(long long)e.completion,(long long)e.recycle,e.flags,e.overlap[0],e.overlap[1],e.overlap[2],c.frame,unsigned(c.kind),unsigned(c.stripe),unsigned(c.x0),unsigned(c.y0),unsigned(c.x1),unsigned(c.y1),c.bytes,unsigned(c.phase),unsigned(c.spi1));}std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}

    if(!std::strcmp(action,"init")){if(!idle)throw std::runtime_error("init requires Idle");census_init(true,20480);return;}
    if(!state)throw std::runtime_error("disp init first");
    if(!std::strcmp(action,"status")){if(!std::strcmp(payload,"layers"))layer_status(out);else status(out);return;}
    if(!std::strcmp(action,"provision-empty")){if(!idle || state->session_active || state->fs_ready || state->provision || !state->flash.write_allowed())throw std::runtime_error("provision requires erased unmounted Idle storage");state->provision_done=false;state->provision=true;while(!state->provision_done)vTaskDelay(1);if(state->provision_error!=Error::None)throw std::runtime_error(error_name(state->provision_error));return;}
    if(!std::strcmp(action,"fault")){unsigned stage=0;if(!idle || std::sscanf(payload,"%u",&stage)!=1 || stage>3)throw std::runtime_error("fault 0..3 requires Idle");state->fault_reached=0;state->fault_stage=stage;return;}
    if(!std::strcmp(action,"store-asset")){unsigned slot=0;char hash[65];if(!idle || state->store_bench || std::sscanf(payload,"%u %64s",&slot,hash)!=2 || slot>=5 || !valid_hash(hash))throw std::runtime_error("store-asset index hash requires Idle");Guard guard;std::strcpy(state->store_assets[slot],hash);return;}
    if(!std::strcmp(action,"store-bench")){unsigned enabled=0;if(std::sscanf(payload,"%u",&enabled)!=1 || enabled>1)throw std::runtime_error("store-bench 0|1");Guard guard;state->store_bench=false;if(enabled){if(!state->allow_call_persistence || !state->defer_persistence)throw std::runtime_error("5b diagnostic controls not armed");for(auto& hash:state->store_assets){auto* entry=state->cache.find(hash);if(!entry || !entry->valid || entry->persisted)throw std::runtime_error("5b needs RAM-only prepared images");}for(auto& item:state->store_cases){item.~StoreCase();new(&item)StoreCase{};}state->store_step=0;++state->store_generation;state->store_start=esp_timer_get_time();state->store_bench=true;}return;}
    if(!std::strcmp(action,"store-results")){char b[1024];lock();size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_STORE_RESULTS {\"steps\":%u,\"items\":[",state->store_step);unsigned n=0;for(auto& item:state->store_cases){at+=std::snprintf(b+at,sizeof(b)-at,"%s[%u,\"%s\",%lld,%lld,%u]",n++?",":"",unsigned(item.kind),error_name(item.error),(long long)item.posted,(long long)item.completed,unsigned(item.bytes));}unlock();std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}
    if(!std::strcmp(action,"defer-persistence")){unsigned enabled=0;if(!idle || std::sscanf(payload,"%u",&enabled)!=1 || enabled>1)throw std::runtime_error("defer-persistence 0|1 requires Idle");state->defer_persistence=enabled;return;}
    if(!std::strcmp(action,"call-persistence")){unsigned enabled=0;if(!idle || std::sscanf(payload,"%u",&enabled)!=1 || enabled>1)throw std::runtime_error("call-persistence 0|1 requires Idle");state->allow_call_persistence=enabled;return;}
    if(!std::strcmp(action,"persisted")){char b[1024];unsigned long long start=0;std::sscanf(payload,"%llu",&start);lock();uint64_t oldest=state->persisted_count>32?state->persisted_count-31:1;if(start<oldest)start=oldest;size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_PERSISTED {\"total\":%llu,\"events\":[",(unsigned long long)state->persisted_count);unsigned n=0;for(uint64_t number=start;number<=state->persisted_count && n<6;++number){auto& event=state->persisted[(number-1)%32];at+=std::snprintf(b+at,sizeof(b)-at,"%s[%llu,\"%s\",\"%s\"]",n++?",":"",(unsigned long long)number,event.hash,error_name(event.error));}unlock();std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}

    if(!std::strcmp(action,"origin")){if(!idle || std::strlen(payload)>=sizeof(state->origin) || !allowed(payload,true))throw std::runtime_error("invalid asset origin");const char* slash=std::strchr(payload+7,'/');if(slash && slash[1])throw std::runtime_error("origin has path");Guard guard;size_t length=slash?size_t(slash-payload):std::strlen(payload);std::memcpy(state->origin,payload,length);state->origin[length]=0;return;}
    if(!std::strcmp(action,"probe")){if(!idle || state->probe_pending || std::strlen(payload)>512 || !allowed(payload,false))throw std::runtime_error("invalid census probe");std::strcpy(state->probe_url,payload);state->probe_pending=true;return;}
    if(!std::strcmp(action,"arena")){if(!idle || state->cache.arena.budget())throw std::runtime_error("arena requires fresh Idle");unsigned bytes=0,F=0,W=0,largest_measured=0;if(std::sscanf(payload,"%u %u %u %u",&bytes,&F,&W,&largest_measured)!=4 || !bytes || bytes>4*1024*1024)throw std::runtime_error("invalid arena budget");size_t available=heap_caps_get_free_size(Psram),largest=heap_caps_get_largest_free_block(Psram);if(F<size_t(W)+589824 || bytes>size_t(F)-W-589824 || bytes>largest_measured || available<589824 || bytes>available-589824 || bytes>largest)throw std::runtime_error("arena reserve floor");Guard guard;void* memory=heap_caps_aligned_alloc(16,bytes,Psram);if(!memory)throw std::bad_alloc();if(!state->cache.arena.init(memory,bytes)){free(memory);throw std::runtime_error("arena init");}return;}
    if(!std::strcmp(action,"buffers")){unsigned n=0;if(!idle || state->mode || state->character_enabled || std::sscanf(payload,"%u",&n)!=1 || (n!=1 && n!=2))throw std::runtime_error("buffers 1|2 requires Idle");Guard guard;if(state->engine.rendering)throw std::runtime_error("renderer busy");state->split=n==2;return;}
    if(!std::strcmp(action,"asset")){unsigned slot=0;char hash[65]{},extra[2]{};if(!idle || state->mode || state->character_enabled || std::sscanf(payload,"%u %64s %1s",&slot,hash,extra)!=2 || slot>=17 || !valid_hash(hash))throw std::runtime_error("asset index hash");Guard guard;std::strcpy(state->assets[slot],hash);return;}
    if(!std::strcmp(action,"bench")){char mode[16];unsigned rate=30;if(std::sscanf(payload,"%15s %u",mode,&rate)<1 || !rate || rate>30)throw std::runtime_error("bench off|cat|full rate");unsigned m=!std::strcmp(mode,"off")?0:!std::strcmp(mode,"cat")?1:!std::strcmp(mode,"full")?2:!std::strcmp(mode,"hold")?3:4;if(m==4)throw std::runtime_error("invalid bench mode");Guard guard;release_demo();state->character_enabled=false;release_bench();state->mode=0;if(m){unsigned count=m==2?17:16;for(unsigned i=0;i<count;++i){if(m==2 && i>0 && i<16)continue;auto* e=state->cache.find(state->assets[i]);if(!e || !state->cache.pin(*e,Pin::Scheduled)){release_bench();throw std::runtime_error("bench asset not-loaded");}state->bench_pins[i]=e;}state->rate=rate;state->bench_frame=0;++state->bench_generation;state->bench_next=esp_timer_get_time();state->mode=m==3?0:m;}return;}
    if(!std::strcmp(action,"measure")){unsigned frames=0;if(!idle || state->mode || state->measure_frames || std::sscanf(payload,"%u",&frames)!=1 || !frames || frames>100)throw std::runtime_error("measure 1..100 requires Idle");state->measure_frames=frames;while(state->measure_frames && !state->dead)vTaskDelay(1);if(state->dead)throw std::runtime_error("display dead; reboot required");return;}
    if(!std::strcmp(action,"qualify")){if(!idle)throw std::runtime_error("qualify requires Idle");unsigned frames=0;if(std::sscanf(payload,"%u",&frames)!=1)throw std::runtime_error("qualify frames");runtime_diagnostic(frames);return;}
    if(!std::strcmp(action,"te")){uint32_t stamps[128];uint32_t n=state->te_count.load(std::memory_order_acquire);unsigned count=std::min(uint32_t(128),n);for(unsigned i=0;i<count;++i)stamps[i]=state->te_stamps[(n-count+i)%128];uint32_t deltas[127];for(unsigned i=1;i<count;++i)deltas[i-1]=stamps[i]-stamps[i-1];if(count>1)std::sort(deltas,deltas+count-1);char b[256];std::snprintf(b,sizeof(b),"\nDISPLAY_TE {\"count\":%lu,\"period_us\":[%lu,%lu,%lu],\"last_device_us32\":%lu,\"basis\":\"GPIO%d-positive-edge;non-IRAM-observer\"}\n",(unsigned long)n,(unsigned long)(count>1?deltas[0]:0),(unsigned long)(count>1?deltas[(count-1)/2]:0),(unsigned long)(count>1?deltas[count-2]:0),(unsigned long)(count?stamps[count-1]:0),te_gpio());out(b);return;}
    if(!std::strcmp(action,"trace")){unsigned start=0;std::sscanf(payload,"%u",&start);char b[1024];size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_TRACE {\"total\":%u,\"start\":%u,\"frames\":[",state->trace_count,start);for(unsigned i=start;i<state->trace_count && i<start+8;++i){auto& t=state->trace[i];at+=std::snprintf(b+at,sizeof(b)-at,"%s[%u,%lld,%lld,%lu]",i==start?"":",",unsigned(t.number),(long long)t.begin,(long long)t.end,(unsigned long)t.crc);}std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);return;}
    if(!std::strcmp(action,"inventory")){unsigned start=0;unsigned long long revision=0;std::sscanf(payload,"%u %llu",&start,&revision);char b[1024];lock();uint64_t current=state->cache.revision+state->flash_revision;if(revision && revision!=current){unlock();throw std::runtime_error("cursor-changed");}size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_INVENTORY {\"epoch\":%llu,\"revision\":%llu,\"items\":[",(unsigned long long)state->engine.epoch,(unsigned long long)current);unsigned n=0,next=256;for(unsigned i=start;i<256;++i){const char* hash=nullptr;size_t bytes=0;bool flash=false,persisted=false,ram=false;unsigned references=0;Error error=Error::None;
        if(i<128){auto& item=state->inventory[i];if(!item.hash[0])continue;hash=item.hash;bytes=item.bytes;flash=item.valid;error=item.error;if(auto* entry=state->cache.find(hash)){ram=entry->valid;references=entry->references();persisted=entry->persisted;}}
        else{auto& entry=state->cache.entries[i-128];if(!entry.valid)continue;bool duplicate=false;for(auto& item:state->inventory)if(item.hash[0] && !std::strcmp(item.hash,entry.hash)){duplicate=true;break;}if(duplicate)continue;hash=entry.hash;bytes=entry.size;ram=true;references=entry.references();persisted=entry.persisted;}
        if(n==6){next=i;break;}at+=std::snprintf(b+at,sizeof(b)-at,"%s[\"%s\",%u,%u,%s,%s,%s,\"%s\"]",n++?",":"",hash,unsigned(bytes),references,ram?"true":"false",flash?"true":"false",persisted?"true":"false",error_name(error));}
        unlock();std::snprintf(b+at,sizeof(b)-at,"],\"next\":%u}\n",next);out(b);return;}

    if(!std::strcmp(action,"result")){char id[33];if(std::sscanf(payload,"%32s",id)!=1)throw std::runtime_error("result id");char b[256];Guard guard;auto* r=state->engine.result(id);if(!r)throw std::runtime_error("result evicted or unknown");std::snprintf(b,sizeof(b),"\nDISPLAY_RESULT {\"id\":\"%s\",\"state\":%u,\"error\":\"%s\",\"revision\":%llu,\"persisted\":%s}\n",id,unsigned(r->state),error_name(r->error),(unsigned long long)r->revision,r->persisted?"true":"false");out(b);return;}
    Meta defaults;{Guard guard;defaults.epoch=state->engine.epoch;defaults.sequence=state->engine.last_sequence+1;std::snprintf(defaults.id,33,"console-%u",++state->console_sequence);}
    auto& q=state->console_request;Error parsed=parse_request(action,payload,std::strlen(payload),&defaults,q);if(parsed!=Error::None)throw std::runtime_error(error_name(parsed));
    auto admitted=post(q);if(admitted.admission!=Admission::Accepted)throw std::runtime_error(error_name(admitted.error));
    // A diagnostic console admission return is distinct from an asynchronous terminal result.
    char b[256];std::snprintf(b,sizeof(b),"\nDISPLAY_POST {\"id\":\"%s\",\"duplicate\":%s}\n",q.meta.id,admitted.duplicate?"true":"false");out(b);
}
}

namespace dg_display {
int idle_configuration_write(int (*write)(void*),void* context) {
    if(!state)return write(context); // No LCD exists yet.
    if(state->session_active.load() || state->dead || flash_mmap_remain())return ESP_ERR_INVALID_STATE;
    if(xSemaphoreTake(state->gate,pdMS_TO_TICKS(2000))!=pdTRUE)return ESP_ERR_TIMEOUT;
    int result=ESP_ERR_INVALID_STATE;
    if(!state->session_active.load() && !state->dead && !flash_mmap_remain())result=write(context);
    configASSERT(xSemaphoreGive(state->gate)==pdTRUE);
    return result;
}
void preload_persisted(const char* hash) {
    if(!state || state->session_active || !valid_hash(hash))throw std::runtime_error("Idle initialized store required");
    Request q{};q.kind=RequestKind::Preload;std::strcpy(q.hash,hash);
    {Guard guard;q.meta.epoch=state->engine.epoch;q.meta.sequence=state->engine.last_sequence+1;
     std::snprintf(q.meta.id,33,"boot-%u",++state->console_sequence);}
    const auto admitted=post(q);
    if(admitted.admission!=Admission::Accepted)throw std::runtime_error(error_name(admitted.error));
    const auto deadline=esp_timer_get_time()+30000000;
    while(esp_timer_get_time()<deadline) {
        {Guard guard;auto* r=state->engine.result(q.meta.id);
         if(!r)throw std::runtime_error("boot result evicted");
         if(r->state!=ResultState::Pending && r->state!=ResultState::Applied) {
             if(r->error!=Error::None || r->state!=ResultState::Completed)throw std::runtime_error(error_name(r->error));
             return;
         }}
        vTaskDelay(pdMS_TO_TICKS(10)+1);
    }
    throw std::runtime_error("persisted preload timeout");
}
namespace {
// Shared with duo_runtime.inc, which is included below.
void allocate_self_arena() {
    char args[128];std::snprintf(args,sizeof(args),"arena %u %u %u %u",SelfArenaBytes,
        unsigned(heap_caps_get_free_size(Psram)),ReconnectWorkingBytes,unsigned(heap_caps_get_largest_free_block(Psram)));
    command(args,true,[](const char*){});
}
}
void standalone_assets(const char* const* hashes,size_t count,bool animate) {
    if(count!=16)throw std::runtime_error("16 character slots required");
    if(!state)census_init(true,20480);
    runtime_wait_ready();
    bool arena_ready=false;{Guard guard;arena_ready=state->cache.arena.budget()!=0;}
    if(!arena_ready)allocate_self_arena();
    for(size_t i=0;i<count;++i) {
        preload_persisted(hashes[i]);
        Guard guard;std::strcpy(state->assets[i],hashes[i]);
    }
    if(animate)command("character on",true,[](const char*){});
}
}

extern "C" bool dg_display_flash_write_allowed(void){return dg_display::physical_write_allowed();}

extern "C" bool dg_display_flash_begin(unsigned op){return dg_display::flash_begin(op);}
extern "C" void dg_display_flash_end(unsigned op,int error){dg_display::flash_end(op,error);}

#if CONFIG_DG_DUO
#include "dg_display/duo_runtime.h"
#include "duo_runtime.inc"
#endif
