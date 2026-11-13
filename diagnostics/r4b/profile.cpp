// Diagnostic overlay only. No production object/library is replaced in place.
#include "profile.h"
#include "esp_cpu.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_memory_utils.h"
#include "esp_intr_alloc.h"
#include "esp_mmu_map.h"
#include "esp_private/periph_ctrl.h"
#include "esp_private/esp_clk.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "hal/timer_ll.h"
#include "soc/interrupts.h"
#include "soc/extmem_reg.h"
#include "soc/soc.h"
#include "mbedtls/ssl.h"
#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <stdexcept>
#include <new>
extern "C" {extern char _instruction_reserved_start[],_instruction_reserved_end[];}

namespace {
constexpr unsigned Pcs=8192, Samples=61000, Callers=256, Ledger=512, Lows=64;
constexpr unsigned Internal=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT;
constexpr unsigned External=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;
struct Slot {TaskHandle_t task=nullptr;volatile uint32_t mask=0;volatile unsigned primary=31;
    uintptr_t alloc_caller=0;unsigned alloc_depth=0;void* observed=nullptr;};
Slot slots[32];portMUX_TYPE slot_mux=portMUX_INITIALIZER_UNLOCKED;
struct Measure {uint64_t cycles=0;unsigned count=0,maximum=0,bins[64]{};};
struct Caller {uintptr_t pc=0;TaskHandle_t owner=nullptr;unsigned caps=0,stage=31,core=0;char task[20]{};
    uint64_t count=0,bytes=0,window_count=0,window_bytes=0;unsigned max_request=0,minimum=UINT32_MAX;};
struct Low {int64_t time=0;uintptr_t pc=0;unsigned free=0,largest=0,bytes=0,caps=0,stage=31;};
struct Dead {int64_t time=0,last_time=0;uint64_t wait=0,last_wait=0,max_wait=0;uintptr_t caller=0;unsigned site=0,core=0,count=0;char task[20]{};};
struct Sample {uint16_t pc;uint8_t flags;} __attribute__((packed));
struct TaskRow {char name[20]{};unsigned id=0,runtime=0;int core=-1;};
struct Data {uint32_t pc_table[Pcs]{};Sample sample[Samples];Measure measures[2][R4bStageCount];
    Caller caller[Callers];uint16_t caller_hash[512];Low lows[Lows];Dead dead[32];
    TaskStatus_t tasks[64];TaskRow task_before[64],task_after[64];uint64_t display_before[13]{},display_after[13]{};};
Data* data=nullptr;std::atomic<bool> allocation_on{false},recording{false},done{false};
std::atomic<unsigned> sample_count{0},pc_lost{0},sample_lost{0};
unsigned caller_count=0,caller_lost=0,ledger_lost=0,low_count=0,dead_count=0,dead_lost=0,skipped_isr=0;
unsigned mhz=240,heap_min=UINT32_MAX,heap_largest_at_min=0,heap_window_min=UINT32_MAX;
uint64_t alloc_observer_cycles=0,alloc_observer_calls=0,stage_observer_cycles=0,stage_observer_calls=0;
uint64_t irq_cycles=0;unsigned irq_max=0,irq_interrupted=0,last_miss=0,shared_miss_intervals=0;
uint32_t inclusive[R4bStageCount]{};uint8_t mapping[512]{};
uint64_t messages[2]{}; // native 1024B callbacks, parsed/dispatched 1024B messages
uint64_t crypto_window[8]{}; // SHA/AES TX starts, PSRAM/internal bytes, RX capacities
uint64_t record_crypto[8]{};unsigned record_count=0;
unsigned successful_record_reads=0;
int64_t start_us=0,end_us=0,tasks_start_us=0,tasks_end_us=0;
unsigned task_before_count=0,task_after_count=0;
portMUX_TYPE alloc_mux=portMUX_INITIALIZER_UNLOCKED,metric_mux=portMUX_INITIALIZER_UNLOCKED,dead_mux=portMUX_INITIALIZER_UNLOCKED;
intr_handle_t irq_handle=nullptr;esp_timer_handle_t stop_timer=nullptr;
TaskHandle_t dump_task=nullptr;
static const char* names[]={"peer_main","dtls_read","record_decrypt","sctp_receive","data_callback","classify",
    "json_parse","rtvi_convert","rtvi_dispatch","app_queue","app_handle","log_enqueue","log_sink",
    "aes","sha","crypto_setup","crypto_wait","cache_sync","socket_recv","socket_send","interrupted_isr","native_message_queue","ssl_read_enclosing_decrypt","client_callback","client_event_queue"};

Slot* slot() {
    if(xPortInIsrContext())return nullptr;
    auto t=xTaskGetCurrentTaskHandle();
    for(auto& s:slots)if(s.task==t)return &s;
    portENTER_CRITICAL(&slot_mux);
    Slot* result=nullptr;
    for(auto& s:slots)if(s.task==t){result=&s;break;}
    if(!result)for(auto& s:slots)if(!s.task){s.task=t;result=&s;break;}
    portEXIT_CRITICAL(&slot_mux);return result;
}
unsigned bin(unsigned us){if(us<4)return us;unsigned e=31-__builtin_clz(us);return std::min(63u,4+(e-2)*4+((us-(1u<<e))>>(e-2)));}
void bounds(unsigned b,unsigned& lo,unsigned& hi){if(b<4){lo=hi=b;return;}auto e=2+(b-4)/4,step=1u<<(e-2);lo=(1u<<e)+(b-4)%4*step;hi=b==63?UINT32_MAX:lo+step-1;}
void quantile(const Measure& m,unsigned percent,unsigned& lo,unsigned& hi){unsigned need=(uint64_t(m.count)*percent+99)/100,total=0;lo=hi=0;if(!need)return;for(unsigned i=0;i<64;++i){total+=m.bins[i];if(total>=need){bounds(i,lo,hi);return;}}}
void add(unsigned core,unsigned stage,uint64_t cycles,unsigned us){portENTER_CRITICAL(&metric_mux);auto& m=data->measures[core][stage];++m.count;m.cycles+=cycles;m.maximum=std::max(m.maximum,us);++m.bins[bin(us)];portEXIT_CRITICAL(&metric_mux);}
void tasks(bool after){unsigned count=uxTaskGetSystemState(data->tasks,64,nullptr);auto* out=after?data->task_after:data->task_before;
    for(unsigned i=0;i<count;++i){auto& t=data->tasks[i];std::snprintf(out[i].name,sizeof(out[i].name),"%s",t.pcTaskName);out[i].id=t.xTaskNumber;out[i].runtime=t.ulRunTimeCounter;out[i].core=xTaskGetCoreID(t.xHandle);}
    if(after){task_after_count=count;tasks_end_us=esp_timer_get_time();}else{task_before_count=count;tasks_start_us=esp_timer_get_time();}}
void stop(void*){recording=false;allocation_on=false;timer_ll_enable_counter(&TIMERG1,0,false);timer_ll_enable_alarm(&TIMERG1,0,false);timer_ll_enable_intr(&TIMERG1,TIMER_LL_EVENT_ALARM(0),false);end_us=esp_timer_get_time();r4b_display_snapshot(data->display_after);tasks(true);done=true;if(dump_task)xTaskNotifyGive(dump_task);}

void IRAM_ATTR interrupt(void*) {
    const uint32_t began=esp_cpu_get_cycle_count();uint32_t pc,ps;
    asm volatile("rsr.epc2 %0":"=a"(pc));asm volatile("rsr.eps2 %0":"=a"(ps));
    timer_ll_clear_intr_status(&TIMERG1,TIMER_LL_EVENT_ALARM(0));
    timer_ll_enable_alarm(&TIMERG1,0,true);
    if(!recording.load(std::memory_order_relaxed))return;
    unsigned primary=31;uint32_t mask=0;
    if((ps&15)!=0){primary=R4bInterruptedIsr;mask=1u<<primary;++irq_interrupted;}
    else{auto task=xTaskGetCurrentTaskHandle();for(auto& s:slots)if(s.task==task){primary=s.primary;mask=s.mask;break;}}
    for(unsigned i=0;i<R4bStageCount;++i)if(mask&(1u<<i))++inclusive[i];
    unsigned region=1;
    if(pc>=0x40370000 && pc<0x40400000)region=0;
    else if(pc>=0x42000000 && pc<0x44000000)region=mapping[(pc-0x42000000)>>16];
    const unsigned misses=REG_READ(EXTMEM_IBUS_ACS_MISS_CNT_REG);bool miss=misses!=last_miss;last_miss=misses;if(miss)++shared_miss_intervals;
    unsigned index=Samples,hash=((pc>>1)*2654435761u)>>(32-13);
    for(unsigned n=0;n<32;++n){unsigned at=(hash+n)&(Pcs-1);auto old=data->pc_table[at];if(old==pc || !old){data->pc_table[at]=pc;index=at;break;}}
    if(index==Samples){++pc_lost;index=65535;}
    auto at=sample_count.load(std::memory_order_relaxed);
    if(at<Samples){data->sample[at]={uint16_t(index),uint8_t((primary<<3)|(region<<1)|unsigned(miss))};sample_count.store(at+1,std::memory_order_release);}else ++sample_lost;
    const unsigned used=esp_cpu_get_cycle_count()-began;irq_cycles+=used;if(used>irq_max)irq_max=used;
}

struct AllocToken {Slot* s=nullptr;bool outer=false;};
AllocToken alloc_enter(uintptr_t pc){if(!allocation_on)return {};if(xPortInIsrContext()){++skipped_isr;return {};}auto* s=slot();if(!s)return {};bool outer=!s->alloc_depth++;if(outer){s->alloc_caller=pc;s->observed=nullptr;}return {s,outer};}
void alloc_leave(AllocToken t){if(t.s){if(t.s->alloc_depth)--t.s->alloc_depth;if(t.outer){t.s->alloc_caller=0;t.s->observed=nullptr;}}}
void forget(void*){} // Caps-tagged allocation counters, not an outstanding-block trace.
void observe(void* ptr,size_t bytes,unsigned caps,uintptr_t pc,Slot* s){
    if(!ptr || !allocation_on || !esp_ptr_internal(ptr) || xPortInIsrContext())return;
    if(s && s->observed==ptr)return;
    if(s)s->observed=ptr;
    const auto began=esp_cpu_get_cycle_count();if(s && s->alloc_caller)pc=s->alloc_caller;
    const unsigned free=heap_caps_get_free_size(Internal);unsigned largest=0;
    if(free<heap_min || (recording && free<heap_window_min))largest=heap_caps_get_largest_free_block(Internal);
    const unsigned stage=s?s->primary:31,core=xPortGetCoreID();const char* name=pcTaskGetName(nullptr);auto task=xTaskGetCurrentTaskHandle();
    portENTER_CRITICAL(&alloc_mux);
    unsigned index=Callers,hash=((unsigned(pc)^unsigned(uintptr_t(task))^(caps*31)^(stage*65537))*2654435761u)>>(32-9),empty=512;
    for(unsigned n=0;n<512;++n){auto at=(hash+n)&511u;auto i=data->caller_hash[at];if(i==65535){empty=at;break;}auto& c=data->caller[i];if(c.pc==pc && c.caps==caps && c.stage==stage && c.owner==task){index=i;break;}}
    if(index==Callers && caller_count<Callers && empty!=512){index=caller_count++;data->caller_hash[empty]=index;auto& c=data->caller[index];c.pc=pc;c.owner=task;c.caps=caps;c.stage=stage;c.core=core;std::snprintf(c.task,sizeof(c.task),"%s",name);}
    if(index==Callers)++caller_lost;
    else{auto& c=data->caller[index];++c.count;c.bytes+=bytes;c.max_request=std::max(c.max_request,unsigned(bytes));if(recording){++c.window_count;c.window_bytes+=bytes;}c.minimum=std::min(c.minimum,free);}
    if(free<heap_min || (recording && free<heap_window_min)){if(low_count<Lows)data->lows[low_count++]={esp_timer_get_time(),pc,free,largest,uint32_t(bytes),caps,stage};heap_min=std::min(heap_min,free);heap_largest_at_min=largest;if(recording)heap_window_min=std::min(heap_window_min,free);}
    ++alloc_observer_calls;alloc_observer_cycles+=uint32_t(esp_cpu_get_cycle_count()-began);
    portEXIT_CRITICAL(&alloc_mux);
}
void measure_json(unsigned core,unsigned stage){auto& m=data->measures[core][stage];unsigned l50,h50,l99,h99;quantile(m,50,l50,h50);quantile(m,99,l99,h99);
    std::printf("\nR4B_STAGE {\"core\":%u,\"stage\":%u,\"name\":\"%s\",\"count\":%u,\"cycles\":%llu,\"mean_us\":%.6f,\"p50_us\":[%u,%u],\"p99_us\":[%u,%u],\"max_us\":%u}\n",core,stage,names[stage],m.count,(unsigned long long)m.cycles,m.count?double(m.cycles)/mhz/m.count:0.0,l50,h50,l99,h99,m.maximum);}
}

R4bToken r4b_enter(unsigned stage){if(!allocation_on)return {};auto* s=slot();if(!s)return {};R4bToken t;t.slot=s;t.stage=stage;t.core=xPortGetCoreID();t.previous=s->mask;t.previous_primary=s->primary;s->mask|=1u<<stage;s->primary=stage;if(recording){t.time=esp_timer_get_time();t.cycle=esp_cpu_get_cycle_count();}return t;}
void r4b_leave(R4bToken t) noexcept {if(!t.slot)return;auto* s=static_cast<Slot*>(t.slot);if(t.time && recording && xPortGetCoreID()==t.core){const auto began=esp_cpu_get_cycle_count();const auto us=unsigned(esp_timer_get_time()-t.time);uint64_t cycles=uint32_t(began-t.cycle);if(uint64_t(us)*mhz>UINT32_MAX)cycles+=((uint64_t(us)*mhz-cycles+(1ull<<31))>>32)<<32;add(t.core,t.stage,cycles,us);portENTER_CRITICAL(&metric_mux);++stage_observer_calls;stage_observer_cycles+=uint32_t(esp_cpu_get_cycle_count()-began);portEXIT_CRITICAL(&metric_mux);}s->mask=t.previous;s->primary=t.previous_primary;}
void r4b_frame(size_t bytes,bool dispatched) noexcept{if(recording && bytes==1024){portENTER_CRITICAL(&metric_mux);++messages[dispatched?1:0];portEXIT_CRITICAL(&metric_mux);}}
void r4b_crypto_transfer(unsigned peripheral,bool tx,uint64_t psram,uint64_t internal) noexcept{if(!recording)return;auto* s=slot();portENTER_CRITICAL(&metric_mux);unsigned base=peripheral==7?0:3;if(tx){++crypto_window[base];crypto_window[base+1]+=psram;crypto_window[base+2]+=internal;}else{crypto_window[6]+=psram;crypto_window[7]+=internal;}if(s && (s->mask&((1u<<R4bRecordDecrypt)|(1u<<R4bSslRead)))){if(tx){++record_crypto[base];record_crypto[base+1]+=psram;record_crypto[base+2]+=internal;}else{record_crypto[6]+=psram;record_crypto[7]+=internal;}}portEXIT_CRITICAL(&metric_mux);}
void r4b_dead(unsigned site,uint64_t wait,uintptr_t caller) noexcept{if(!data)return;portENTER_CRITICAL_SAFE(&dead_mux);Dead* target=nullptr;for(unsigned i=0;i<dead_count;++i)if(data->dead[i].site==site){target=&data->dead[i];break;}if(!target && dead_count<32){target=&data->dead[dead_count++];target->time=esp_timer_get_time();target->wait=wait;target->site=site;target->core=xPortGetCoreID();target->caller=caller;if(!xPortInIsrContext())std::snprintf(target->task,sizeof(target->task),"%s",pcTaskGetName(nullptr));}if(target){++target->count;target->last_time=esp_timer_get_time();target->last_wait=wait;target->max_wait=std::max(target->max_wait,wait);}else ++dead_lost;portEXIT_CRITICAL_SAFE(&dead_mux);}

static void dump_worker(void*){for(;;){ulTaskNotifyTake(pdTRUE,portMAX_DELAY);r4b_command("status");r4b_command("display");r4b_command("tasks");r4b_command("stages");r4b_command("crypto");r4b_command("dead");r4b_command("lows");char cmd[64];for(unsigned i=0;i<caller_count;i+=8){std::snprintf(cmd,sizeof(cmd),"callers %u",i);r4b_command(cmd);}for(unsigned i=0;i<Pcs;i+=64){std::snprintf(cmd,sizeof(cmd),"dictionary %u",i);r4b_command(cmd);}for(unsigned i=0;i<sample_count.load();i+=128){std::snprintf(cmd,sizeof(cmd),"samples %u",i);r4b_command(cmd);}std::printf("\nR4B_DUMP_DONE {\"samples\":%u}\n",sample_count.load());}}

void r4b_init(){if(data)return;data=static_cast<Data*>(heap_caps_calloc(1,sizeof(Data),External));if(!data)throw std::bad_alloc();new(data)Data{};std::memset(data->caller_hash,0xff,sizeof(data->caller_hash));mhz=esp_clk_cpu_freq()/1000000;
    const auto map_begin=uintptr_t(_instruction_reserved_start)&~uintptr_t(65535),map_end=(uintptr_t(_instruction_reserved_end)+65535)&~uintptr_t(65535);
    for(unsigned i=0;i<512;++i){uintptr_t address=0x42000000+i*65536;mapping[i]=2;if(address<map_begin || address>=map_end)continue;esp_paddr_t physical;mmu_target_t target;auto err=esp_mmu_vaddr_to_paddr(reinterpret_cast<void*>(address),&physical,&target);mapping[i]=err==ESP_OK && target==MMU_TARGET_PSRAM0?3:2;}
    periph_module_enable(PERIPH_TIMG1_MODULE);timer_ll_enable_counter(&TIMERG1,0,false);timer_ll_set_clock_source(1,0,GPTIMER_CLK_SRC_APB);timer_ll_set_clock_prescale(&TIMERG1,0,80);timer_ll_set_count_direction(&TIMERG1,0,GPTIMER_COUNT_UP);timer_ll_enable_auto_reload(&TIMERG1,0,true);timer_ll_set_reload_value(&TIMERG1,0,0);timer_ll_set_alarm_value(&TIMERG1,0,1000);timer_ll_enable_intr(&TIMERG1,TIMER_LL_EVENT_ALARM(0),false);
    // Non-IRAM interrupt registration masks it during cache-disabled flash work;
    // the handler itself is IRAM, and steady profiling performs no flash work.
    ESP_ERROR_CHECK(esp_intr_alloc(ETS_TG1_T0_LEVEL_INTR_SOURCE,ESP_INTR_FLAG_LEVEL2,interrupt,nullptr,&irq_handle));
    esp_timer_create_args_t args{};args.callback=stop;args.name="r4b-end";ESP_ERROR_CHECK(esp_timer_create(&args,&stop_timer));
    if(xTaskCreatePinnedToCoreWithCaps(dump_worker,"r4b-dump",8192,nullptr,1,&dump_task,1,External)!=pdPASS)throw std::bad_alloc();
    REG_WRITE(EXTMEM_IBUS_TO_FLASH_START_VADDR_REG,0x42000000);REG_WRITE(EXTMEM_IBUS_TO_FLASH_END_VADDR_REG,0x43ffffff);
    heap_min=heap_caps_get_free_size(Internal);allocation_on=true;
    std::printf("\nR4B_INIT {\"psram_bytes\":%u,\"frequency_hz\":1000,\"core\":0,\"irq_level\":2,\"pc_source\":\"hardware_EPC_2\",\"cpu_mhz\":%u,\"pc_capacity\":%u,\"dictionary_capacity\":%u}\n",unsigned(sizeof(Data)),mhz,Samples,Pcs);
}
void r4b_command(const char* args){while(*args==' ')++args;char action[24]{};unsigned start=0;if(std::sscanf(args,"%23s %u",action,&start)<1)throw std::runtime_error("profile action required");
    if(!std::strcmp(action,"start")){if(recording || done)throw std::runtime_error("one60sprofile per freshboot");std::memset(data->measures,0,sizeof(data->measures));alloc_observer_cycles=alloc_observer_calls=0;stage_observer_cycles=stage_observer_calls=0;for(unsigned i=0;i<caller_count;++i){auto& c=data->caller[i];c.window_count=c.window_bytes=0;}heap_window_min=heap_caps_get_free_size(Internal);r4b_display_snapshot(data->display_before);tasks(false);last_miss=REG_READ(EXTMEM_IBUS_ACS_MISS_CNT_REG);start_us=esp_timer_get_time();recording=true;timer_ll_trigger_soft_reload(&TIMERG1,0);timer_ll_clear_intr_status(&TIMERG1,TIMER_LL_EVENT_ALARM(0));timer_ll_enable_intr(&TIMERG1,TIMER_LL_EVENT_ALARM(0),true);timer_ll_enable_alarm(&TIMERG1,0,true);timer_ll_enable_counter(&TIMERG1,0,true);ESP_ERROR_CHECK(esp_timer_start_once(stop_timer,60000000));std::printf("\nR4B_STARTED {\"device_us\":%lld,\"duration_us\":60000000}\n",(long long)start_us);return;}
    if(!std::strcmp(action,"status")){std::printf("\nR4B_PROFILE {\"recording\":%s,\"done\":%s,\"start_us\":%lld,\"end_us\":%lld,\"samples\":%u,\"pc_lost\":%u,\"sample_lost\":%u,\"irq_body_cycles\":%llu,\"irq_body_max_cycles\":%u,\"interrupted_isr\":%u,\"shared_miss_intervals\":%u,\"heap_boot_observed_min\":%u,\"heap_window_observed_min\":%u,\"heap_largest_at_last_low\":%u,\"caller_count\":%u,\"caller_lost\":%u,\"ledger_lost\":%u,\"skipped_alloc_isr\":%u,\"alloc_observer_cycles\":%llu,\"alloc_observer_calls\":%llu,\"stage_observer_cycles\":%llu,\"stage_observer_calls\":%llu,\"native_1024_messages\":%llu,\"dispatched_1024_messages\":%llu,\"dead_count\":%u,\"dead_lost\":%u}\n",recording?"true":"false",done?"true":"false",(long long)start_us,(long long)end_us,sample_count.load(),pc_lost.load(),sample_lost.load(),(unsigned long long)irq_cycles,irq_max,irq_interrupted,shared_miss_intervals,heap_min,heap_window_min,heap_largest_at_min,caller_count,caller_lost,ledger_lost,skipped_isr,(unsigned long long)alloc_observer_cycles,(unsigned long long)alloc_observer_calls,(unsigned long long)stage_observer_cycles,(unsigned long long)stage_observer_calls,(unsigned long long)messages[0],(unsigned long long)messages[1],dead_count,dead_lost);return;}
    if(!std::strcmp(action,"dead")){for(unsigned i=0;i<dead_count;++i){auto& d=data->dead[i];std::printf("\nR4B_DEAD {\"index\":%u,\"site\":%u,\"device_us\":%lld,\"wait_us\":%llu,\"last_us\":%lld,\"last_wait_us\":%llu,\"max_wait_us\":%llu,\"count\":%u,\"caller_pc\":%u,\"core\":%u,\"task\":\"%s\"}\n",i,d.site,(long long)d.time,(unsigned long long)d.wait,(long long)d.last_time,(unsigned long long)d.last_wait,(unsigned long long)d.max_wait,d.count,unsigned(d.caller),d.core,d.task);}return;}
    if(!std::strcmp(action,"display")){char b[1200];size_t at=std::snprintf(b,sizeof(b),"\nR4B_DISPLAY {\"before\":[");for(unsigned i=0;i<13;++i)at+=std::snprintf(b+at,sizeof(b)-at,"%s%llu",i?",":"",(unsigned long long)data->display_before[i]);at+=std::snprintf(b+at,sizeof(b)-at,"],\"after\":[");for(unsigned i=0;i<13;++i)at+=std::snprintf(b+at,sizeof(b)-at,"%s%llu",i?",":"",(unsigned long long)data->display_after[i]);std::snprintf(b+at,sizeof(b)-at,"]}\n");std::printf("%s",b);return;}
    if(recording || !done)throw std::runtime_error("profile dump requires ended60swindow");
    if(!std::strcmp(action,"stages")){for(unsigned core=0;core<2;++core)for(unsigned i=0;i<R4bStageCount;++i)measure_json(core,i);std::printf("\nR4B_INCLUSIVE {\"counts\":[");for(unsigned i=0;i<R4bStageCount;++i)std::printf("%s%u",i?",":"",unsigned(inclusive[i]));std::printf("]}\n");return;}
    if(!std::strcmp(action,"tasks")){for(unsigned i=0;i<task_after_count;++i){auto& b=data->task_after[i];TaskRow* a=nullptr;for(unsigned j=0;j<task_before_count;++j)if(data->task_before[j].id==b.id){a=&data->task_before[j];break;}std::printf("\nR4B_TASK {\"name\":\"%s\",\"id\":%u,\"core\":%d,\"before_us\":%lld,\"after_us\":%lld,\"runtime_delta_us\":%u,\"new_task\":%s}\n",b.name,b.id,b.core,(long long)tasks_start_us,(long long)tasks_end_us,a?unsigned(b.runtime-a->runtime):b.runtime,a?"false":"true");}return;}
    if(!std::strcmp(action,"samples")){unsigned count=sample_count.load();char b[2600];size_t at=std::snprintf(b,sizeof(b),"\nR4B_SAMPLES {\"start\":%u,\"total\":%u,\"items\":[",start,count);for(unsigned i=start;i<count && i<start+128;++i){auto s=data->sample[i];at+=std::snprintf(b+at,sizeof(b)-at,"%s[%u,%u]",i==start?"":",",unsigned(s.pc),unsigned(s.flags));}std::snprintf(b+at,sizeof(b)-at,"]}\n");std::printf("%s",b);return;}
    if(!std::strcmp(action,"dictionary")){char b[2000];size_t at=std::snprintf(b,sizeof(b),"\nR4B_DICTIONARY {\"start\":%u,\"total\":%u,\"items\":[",start,Pcs);for(unsigned i=start;i<Pcs && i<start+64;++i)at+=std::snprintf(b+at,sizeof(b)-at,"%s%u",i==start?"":",",unsigned(data->pc_table[i]));std::snprintf(b+at,sizeof(b)-at,"]}\n");std::printf("%s",b);return;}
    if(!std::strcmp(action,"callers")){for(unsigned i=start;i<caller_count && i<start+8;++i){auto& c=data->caller[i];std::printf("\nR4B_ALLOC {\"index\":%u,\"total\":%u,\"caller_pc\":%u,\"caps\":%u,\"stage\":%u,\"core\":%u,\"task\":\"%s\",\"count\":%llu,\"requested_bytes\":%llu,\"window_count\":%llu,\"window_requested_bytes\":%llu,\"max_request_bytes\":%u,\"observed_free_min\":%u}\n",i,caller_count,unsigned(c.pc),c.caps,c.stage,c.core,c.task,(unsigned long long)c.count,(unsigned long long)c.bytes,(unsigned long long)c.window_count,(unsigned long long)c.window_bytes,c.max_request,c.minimum);}return;}
    if(!std::strcmp(action,"lows")){for(unsigned i=0;i<low_count;++i){auto& l=data->lows[i];std::printf("\nR4B_LOW {\"index\":%u,\"device_us\":%lld,\"caller_pc\":%u,\"free\":%u,\"largest\":%u,\"bytes\":%u,\"caps\":%u,\"stage\":%u}\n",i,(long long)l.time,unsigned(l.pc),l.free,l.largest,l.bytes,l.caps,l.stage);}return;}
    if(!std::strcmp(action,"crypto")){std::printf("\nR4B_CRYPTO {\"record_count\":%u,\"successful_record_reads\":%u,\"all_window\":[",record_count,successful_record_reads);for(unsigned i=0;i<8;++i)std::printf("%s%llu",i?",":"",(unsigned long long)crypto_window[i]);std::printf("],\"in_record_decrypt\":[");for(unsigned i=0;i<8;++i)std::printf("%s%llu",i?",":"",(unsigned long long)record_crypto[i]);std::printf("]}\n");return;}
    throw std::runtime_error("profile start|status|dead|stages|tasks|samples|dictionary|callers|lows|crypto");
}

extern "C" {
void* __real_heap_caps_malloc_base(size_t,unsigned);
void* __wrap_heap_caps_malloc_base(size_t n,unsigned caps){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));auto* p=__real_heap_caps_malloc_base(n,caps);observe(p,n,caps,uintptr_t(__builtin_return_address(0)),t.s);alloc_leave(t);return p;}
void* __real_heap_caps_calloc_base(size_t,size_t,unsigned);
void* __wrap_heap_caps_calloc_base(size_t n,size_t z,unsigned caps){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));auto* p=__real_heap_caps_calloc_base(n,z,caps);observe(p,n*z,caps,uintptr_t(__builtin_return_address(0)),t.s);alloc_leave(t);return p;}
void* __real_heap_caps_realloc_base(void*,size_t,unsigned);
void* __wrap_heap_caps_realloc_base(void* old,size_t n,unsigned caps){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));auto* p=__real_heap_caps_realloc_base(old,n,caps);if(p || !n)forget(old);observe(p,n,caps,uintptr_t(__builtin_return_address(0)),t.s);alloc_leave(t);return p;}
void* __real_heap_caps_aligned_alloc_base(size_t,size_t,unsigned);
void* __wrap_heap_caps_aligned_alloc_base(size_t a,size_t n,unsigned caps){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));auto* p=__real_heap_caps_aligned_alloc_base(a,n,caps);observe(p,n,caps,uintptr_t(__builtin_return_address(0)),t.s);alloc_leave(t);return p;}
void __real_heap_caps_free(void*);
void __wrap_heap_caps_free(void* p){forget(p);__real_heap_caps_free(p);}
void* __real_malloc(size_t);
void* __wrap_malloc(size_t n){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));auto* p=__real_malloc(n);observe(p,n,MALLOC_CAP_DEFAULT,uintptr_t(__builtin_return_address(0)),t.s);alloc_leave(t);return p;}
void* __real_calloc(size_t,size_t);
void* __wrap_calloc(size_t n,size_t z){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));auto* p=__real_calloc(n,z);observe(p,n*z,MALLOC_CAP_DEFAULT,uintptr_t(__builtin_return_address(0)),t.s);alloc_leave(t);return p;}
void* __real_realloc(void*,size_t);
void* __wrap_realloc(void* old,size_t n){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));auto* p=__real_realloc(old,n);if(p || !n)forget(old);observe(p,n,MALLOC_CAP_DEFAULT,uintptr_t(__builtin_return_address(0)),t.s);alloc_leave(t);return p;}
void __real_free(void*);
void __wrap_free(void* p){forget(p);__real_free(p);}
void* __real__Znwj(size_t);
void* __wrap__Znwj(size_t n){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));try{auto* p=__real__Znwj(n);alloc_leave(t);return p;}catch(...){alloc_leave(t);throw;}}
void* __real__Znaj(size_t);
void* __wrap__Znaj(size_t n){auto t=alloc_enter(uintptr_t(__builtin_return_address(0)));try{auto* p=__real__Znaj(n);alloc_leave(t);return p;}catch(...){alloc_leave(t);throw;}}
int __real_mbedtls_ssl_decrypt_buf(const mbedtls_ssl_context*,void*,void*);
int __wrap_mbedtls_ssl_decrypt_buf(const mbedtls_ssl_context* s,void* t,void* r){R4B_SCOPE(R4bRecordDecrypt);if(recording)++record_count;return __real_mbedtls_ssl_decrypt_buf(s,t,r);}
int __real_mbedtls_ssl_read(mbedtls_ssl_context*,unsigned char*,size_t);
int __wrap_mbedtls_ssl_read(mbedtls_ssl_context* s,unsigned char* b,size_t n){R4B_SCOPE(R4bSslRead);auto result=__real_mbedtls_ssl_read(s,b,n);if(recording && result>0)++successful_record_reads;return result;}
int __real_sctp_incoming_data(void*,uint8_t*,int);
int __wrap_sctp_incoming_data(void* s,uint8_t* b,int n){R4B_SCOPE(R4bSctp);return __real_sctp_incoming_data(s,b,n);}
void __real_aes_hal_wait_done();
void __wrap_aes_hal_wait_done(){R4B_SCOPE(R4bCryptoWait);__real_aes_hal_wait_done();}
void __real_sha_hal_wait_idle();
void __wrap_sha_hal_wait_idle(){R4B_SCOPE(R4bCryptoWait);__real_sha_hal_wait_idle();}
int __real_esp_cache_msync(void*,size_t,int);
int __wrap_esp_cache_msync(void* p,size_t n,int flags){R4B_SCOPE(R4bCacheSync);return __real_esp_cache_msync(p,n,flags);}
}
