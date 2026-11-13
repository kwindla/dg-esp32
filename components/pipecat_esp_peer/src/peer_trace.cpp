#include "peer_trace.h"
#include "sdkconfig.h"
#include "dtls_record.h"
#include "socket_select_policy.h"
#include "handshake_policy.h"
#include "mbedtls/ssl.h"
#include "esp_cpu.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "multi_heap.h"
#include "lwip/sockets.h"
#include "lwip/sys.h"
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <new>
#include <mutex>
namespace pipecat::esp_peer {
namespace {
std::mutex stat_mutex;
multi_heap_handle_t stat_heap=nullptr;
constexpr size_t stat_arena_bytes=192*1024;
}
void stat_init() {
    std::lock_guard<std::mutex> lock(stat_mutex);
    if(stat_heap)return;
    auto* buffer=heap_caps_malloc(stat_arena_bytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if(!buffer)throw std::bad_alloc();
    stat_heap=multi_heap_register(buffer,stat_arena_bytes);
    if(!stat_heap) {free(buffer);throw std::bad_alloc();}
}
void* stat_allocate(size_t bytes) {
    stat_init();std::lock_guard<std::mutex> lock(stat_mutex);
    return multi_heap_malloc(stat_heap,bytes);
}
void stat_free(void* pointer) {
    std::lock_guard<std::mutex> lock(stat_mutex);multi_heap_free(stat_heap,pointer);
}
thread_local PeerTrace* active_peer_trace=nullptr;
PeerTrace::PeerTrace(std::shared_ptr<ArrivalRing> ring,uint32_t generation,bool detailed)
    :measures(detailed,[](size_t bytes)->void* {
        return heap_caps_malloc(bytes,MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    },std::free),full_call_timing(detailed),ring_(std::move(ring)),generation_(generation) {}
std::shared_ptr<PeerTrace> make_peer_trace(std::shared_ptr<ArrivalRing> ring,uint32_t generation,bool detailed) {
    // Hot profiling counters must not compete with codecs/queues for the
    // external-memory bus. Detailed counters are allocated only on request;
    // the always-enabled iteration counter remains inside this object.
    void* memory=heap_caps_malloc(sizeof(PeerTrace),MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    if(!memory)throw std::bad_alloc();
    PeerTrace* trace;
    try {trace=new(memory)PeerTrace(std::move(ring),generation,detailed);}
    catch(...) {free(memory);throw;}
    return std::shared_ptr<PeerTrace>(trace,[](PeerTrace* p){p->~PeerTrace();free(p);});
}
void PeerTrace::notify_owner() {
    if(!event_wake)return;
    ++event_notifications;
    std::lock_guard<std::mutex> lock(wake_mutex_);
    work_pending_=true;
    if(wait_sem_)sys_sem_signal(static_cast<sys_sem_t*>(wait_sem_));
}
void PeerTrace::arm_wait(void* semaphore) {
    ++event_wait_calls;
    std::lock_guard<std::mutex> lock(wake_mutex_);
    wait_sem_=semaphore;
    if(work_pending_)sys_sem_signal(static_cast<sys_sem_t*>(wait_sem_));
}
void PeerTrace::disarm_wait() {
    std::lock_guard<std::mutex> lock(wake_mutex_);wait_sem_=nullptr;
}
void PeerTrace::acknowledge_work() {
    if(!event_wake)return;
    std::lock_guard<std::mutex> lock(wake_mutex_);work_pending_=false;
}
void ArrivalRing::enable() {
    if(rows_)return;
    rows_=static_cast<ArrivalRecord*>(heap_caps_calloc(capacity,sizeof(ArrivalRecord),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(!rows_) throw std::bad_alloc();
}
ArrivalRing::~ArrivalRing() {free(rows_);}
void ArrivalRing::record(ArrivalRecord row) {
    if(!rows_)return;
    portENTER_CRITICAL(&mux_);
    if(head_-tail_==capacity) {++tail_;++overwritten_;}
    rows_[head_++%capacity]=row;
    portEXIT_CRITICAL(&mux_);
}
size_t ArrivalRing::drain(ArrivalRecord* rows,size_t n) {
    portENTER_CRITICAL(&mux_);
    n=std::min({n,size_t(head_-tail_),size_t(64)});
    for(size_t i=0;i<n;++i) rows[i]=rows_[tail_++%capacity];
    portEXIT_CRITICAL(&mux_);
    return n;
}
void PeerTrace::record(ArrivalRecord row) {if(!arrival_trace)return;row.reserved=uint16_t(generation_);ring_->record(row);}
size_t PeerTrace::drain(ArrivalRecord* rows,size_t n) {return ring_->drain(rows,n);}
StatJson ArrivalRing::stats() const {
    portENTER_CRITICAL(&mux_);
    const auto head=head_,tail=tail_,overwritten=overwritten_;
    portEXIT_CRITICAL(&mux_);
    return {{"allocated",rows_!=nullptr},{"written",head},{"pending",head-tail},{"overwritten",overwritten},{"capacity",capacity},{"record_bytes",sizeof(ArrivalRecord)},{"version",1}};
}
void PeerTrace::mark(unsigned kind,uint32_t value,uint32_t aux) {
    if(!arrival_trace)return;
    record({esp_timer_get_time(),0,0,value,aux,0,0,uint8_t(kind),0,0});
}
void PeerTrace::rtp(unsigned kind,const void* data,int n) {
    if(!arrival_trace)return;
    if(n<12 || !data) return;
    auto* p=static_cast<const uint8_t*>(data);
    if(p[0]>>6!=2 || (p[1]>=192 && p[1]<=223)) return; // exclude RTCP
    auto u32=[](const uint8_t* b) {return uint32_t(b[0])<<24|uint32_t(b[1])<<16|uint32_t(b[2])<<8|b[3];};
    auto time=esp_timer_get_time();auto ssrc=u32(p+8);uint16_t seq=uint16_t(p[2]<<8|p[3]);
    // Keep the high-water sequence across reordered/duplicate packets and wrap.
    if(kind==Wire) {
        if(!rtp_seen_ || ssrc!=rtp_ssrc_) {rtp_seen_=true;rtp_ssrc_=ssrc;rtp_highest_=seq;}
        else {
            int16_t delta=int16_t(seq-rtp_highest_);
            if(delta>1)record({time,u32(p+4),ssrc,uint32_t(delta-1),uint32_t(rtp_highest_),seq,uint16_t(n),RtpGap,0,0});
            if(delta>0)rtp_highest_=seq;
        }
    }
    record({time,u32(p+4),ssrc,0,0,seq,uint16_t(n),uint8_t(kind),0,0});
}
StatJson PeerTrace::stats() const {
    static const char* names[]={"main_loop","send_audio","send_data","control","wait","socket_recv","socket_send","srtp_decrypt","srtp_encrypt","dtls_read","dtls_write","socket_select","lwip_select_nested","loop_iteration","data_callback","heap_sample"};
    StatJson result={{"iterations",iterations.load()},{"empty_iterations",empty_iterations.load()},
        {"cycle_clock_mhz",240},{"inclusive_intervals",true},
        {"full_call_timing",full_call_timing},
        {"part_histogram","power-of-two microsecond buckets; quantiles are upper bounds; mean/max exact"},
        {"hot_counter_bytes",sizeof(PeerTrace)},
        {"optional_counter_bytes",full_call_timing?measures.optional_bytes():0},
        {"direct_select_calls",direct_select_calls.load()},
        {"socket_send_errors",socket_send_errors.load()},
        {"dtls_app_datagrams",dtls_app_datagrams.load()},
        {"socket_send_enomem",socket_send_enomem.load()},
        {"socket_send_eagain",socket_send_eagain.load()},
        {"socket_send_last_errno",socket_send_last_errno.load()},
        {"event_wait_calls",event_wait_calls.load()},{"event_notifications",event_notifications.load()}};
    result["receive_budget_yields"]=receive_budget_yields.load();
    result["prechat_receive_budget_activations"]=prechat_receive_budget_activations.load();
    for(unsigned i=0;i<unsigned(PeerPart::Count);++i) {
        if(!full_call_timing && i!=unsigned(PeerPart::Iteration)) {
            result[names[i]]={{"enabled",false},{"calls",nullptr},{"cycles",nullptr},
                {"wall_us",nullptr},{"mean_us",nullptr},{"p50_us",nullptr},
                {"p95_us",nullptr},{"max_us",nullptr}};
            continue;
        }
        const auto& m=measures[i];unsigned n=m.histogram.count.load();
        auto [cycles,wall]=m.totals.read();
        result[names[i]]={{"enabled",true},{"calls",n},{"cycles",cycles},{"wall_us",wall},
            {"mean_us",wall/std::max(1u,n)},{"p50_us",m.histogram.quantile(50)},
            {"p95_us",m.histogram.quantile(95)},{"max_us",m.histogram.maximum.load()}};
    }
    result["trace"]=ring_->stats();
    result["trace"]["enabled"]=arrival_trace;
    return result;
}
PeerTimer::PeerTimer(PeerPart part):trace_(active_peer_trace),part_(part),cycles_(0),time_(0) {
    if(trace_ && (trace_->full_call_timing || part==PeerPart::Iteration) &&
       trace_->owner==xTaskGetCurrentTaskHandle()) {time_=esp_timer_get_time();cycles_=esp_cpu_get_cycle_count();}
    else trace_=nullptr;
}
PeerTimer::~PeerTimer() {
    if(trace_) {
        auto elapsed=unsigned(esp_timer_get_time()-time_);
        trace_->measures[unsigned(part_)].add(esp_cpu_get_cycle_count()-cycles_,elapsed);
        if(part_==PeerPart::Iteration && elapsed>40000)trace_->mark(PeerTrace::SlowLoop,elapsed);
        else if(trace_->full_call_timing && elapsed>=40000)
            trace_->mark(PeerTrace::SlowCall,elapsed,unsigned(part_));
    }
}
static PeerTrace* current() {
    // Other tasks use socket APIs too: gate by core first, then owner handle.
    return xPortGetCoreID()==0 && active_peer_trace && active_peer_trace->owner==xTaskGetCurrentTaskHandle()?active_peer_trace:nullptr;
}
}
using namespace pipecat::esp_peer;
extern "C" {
int __real_mbedtls_ssl_handshake(mbedtls_ssl_context*);
int __wrap_mbedtls_ssl_handshake(mbedtls_ssl_context* ssl) {
    auto* t=current();
    // A failed native client handshake otherwise gets called again in its
    // invalid SSL state. Keep cancellation inside the WANT_READ/WRITE loop
    // so disconnect can join the owner without touching native state abroad.
    int result=t && handshake_cancelled(t->stopping && t->stopping->load(),
        esp_timer_get_time(),t->connect_deadline_us)
        ? MBEDTLS_ERR_SSL_TIMEOUT : __real_mbedtls_ssl_handshake(ssl);
    if(t && handshake_terminal(result,MBEDTLS_ERR_SSL_WANT_READ,
        MBEDTLS_ERR_SSL_WANT_WRITE,MBEDTLS_ERR_SSL_HELLO_VERIFY_REQUIRED))t->handshake_failed=true;
    return result;
}
int __real_select(int,fd_set*,fd_set*,fd_set*,struct timeval*);
int __real_lwip_select(int,fd_set*,fd_set*,fd_set*,struct timeval*);
int __wrap_select(int nfds,fd_set* read,fd_set* write,fd_set* error,struct timeval* timeout) {
    PeerTimer timer(PeerPart::Select);
    if(auto* t=current();t && t->direct_socket_select && nfds>0 && nfds<=FD_SETSIZE) {
        // Preserve the general VFS path for every other task and any mixed
        // descriptor set. Native peer UDP/TCP sets contain sockets only.
        if(sockets_only(nfds,read,write,error,LWIP_SOCKET_OFFSET)) {
            // A native receive drain must yield to the same owner's audio
            // sends. Return normal select timeout without consuming packets.
            // Setup, writes and other tasks retain their original behavior.
            timeval bounded{};
            if(bound_receive_select(t->receive_budget.deadline_us,esp_timer_get_time(),
                    read,write,error,timeout,bounded)==0) {
                ++t->receive_budget_yields;return 0;
            }
            ++t->direct_select_calls;PeerTimer nested(PeerPart::LwipSelect);
            t->selecting_read=t->event_wake && read && !write;
            int result=__real_lwip_select(nfds,read,write,error,timeout);
            t->selecting_read=false;return result;
        }
    }
    return __real_select(nfds,read,write,error,timeout);
}
u32_t __real_sys_arch_sem_wait(sys_sem_t*,u32_t);
u32_t __wrap_sys_arch_sem_wait(sys_sem_t* semaphore,u32_t timeout) {
    auto* t=current();
    if(!t || !t->selecting_read)return __real_sys_arch_sem_wait(semaphore,timeout);
    t->arm_wait(semaphore);
    auto result=__real_sys_arch_sem_wait(semaphore,timeout);
    t->disarm_wait();return result;
}
int __wrap_lwip_select(int nfds,fd_set* read,fd_set* write,fd_set* error,struct timeval* timeout) {
    PeerTimer timer(PeerPart::LwipSelect);
    timeval bounded{};
    if(auto* t=current();t && bound_receive_select(t->receive_budget.deadline_us,
            esp_timer_get_time(),read,write,error,timeout,bounded)==0) {
        ++t->receive_budget_yields;return 0;
    }
    return __real_lwip_select(nfds,read,write,error,timeout);
}
ssize_t __real_lwip_recvfrom(int,void*,size_t,int,struct sockaddr*,socklen_t*);
ssize_t __wrap_lwip_recvfrom(int s,void* data,size_t len,int flags,struct sockaddr* from,socklen_t* fromlen) {
    PeerTimer timer(PeerPart::Recv);
    if(auto* t=current();t && t->receive_budget.exhausted(esp_timer_get_time())) {
        ++t->receive_budget_yields;errno=EWOULDBLOCK;return -1;
    }
    auto ret=__real_lwip_recvfrom(s,data,len,flags,from,fromlen);
    if(auto* t=current()) {
        if(ret>=0)t->receive_budget.received(unsigned(ret)); // Zero-length UDP also consumes a read.
#if CONFIG_DG_PEER_DTLS_RX_TRACE
        DtlsRecordIdentity id;
        if(ret>0 && dtls_application(data,size_t(ret),id)) {
            auto count=++t->dtls_app_datagrams;
            // timestamp/ssrc = low32/high16 record sequence, seq = DTLS epoch.
            t->record({esp_timer_get_time(),uint32_t(id.sequence),uint32_t(id.sequence>>32),
                count,uint32_t(s),id.epoch,uint16_t(ret),PeerTrace::DtlsApplication,
                uint8_t(id.complete?0:1),0});
        }
#endif
        t->rtp(PeerTrace::Wire,data,int(ret));
    }
    return ret;
}
int __real_dtls_srtp_decrypt_rtp_packet(void*,uint8_t*,int*);
int __wrap_dtls_srtp_decrypt_rtp_packet(void* ctx,uint8_t* p,int* n) {
    int result;
    {PeerTimer timer(PeerPart::Decrypt);result=__real_dtls_srtp_decrypt_rtp_packet(ctx,p,n);}
    if(result==0) {if(auto* t=current()) t->rtp(PeerTrace::Authenticated,p,*n);}
    return result;
}
void __real_dtls_srtp_encrypt_rtp_packet(void*,uint8_t*,int,int*);
void __wrap_dtls_srtp_encrypt_rtp_packet(void* ctx,uint8_t* p,int capacity,int* n) {
    PeerTimer timer(PeerPart::Encrypt);__real_dtls_srtp_encrypt_rtp_packet(ctx,p,capacity,n);
}
int __real_dtls_srtp_read(void*,uint8_t*,size_t);
int __wrap_dtls_srtp_read(void* ctx,uint8_t* p,size_t n) {
    PeerTimer timer(PeerPart::DtlsRead);return __real_dtls_srtp_read(ctx,p,n);
}
int __real_dtls_srtp_write(void*,const uint8_t*,size_t);
int __wrap_dtls_srtp_write(void* ctx,const uint8_t* p,size_t n) {
    PeerTimer timer(PeerPart::DtlsWrite);return __real_dtls_srtp_write(ctx,p,n);
}
// Called by the existing zero-datagram guard, preserving its exact behavior.
ssize_t pipecat_profile_sendto(int s,const void* p,size_t n,int flags,const struct sockaddr* to,socklen_t len);
ssize_t __real_lwip_sendto(int,const void*,size_t,int,const struct sockaddr*,socklen_t);
ssize_t pipecat_profile_sendto(int s,const void* p,size_t n,int flags,const struct sockaddr* to,socklen_t len) {
    PeerTimer timer(PeerPart::Send);
    auto result=__real_lwip_sendto(s,p,n,flags,to,len);
    if(result>0) {if(auto* t=current())t->rtp(PeerTrace::UplinkWire,p,int(result));}
    if(result<0) {
        const int error=errno;
        if(auto* t=current()) {
            ++t->socket_send_errors;t->socket_send_last_errno=error;
            if(error==ENOMEM)++t->socket_send_enomem;
            if(error==EAGAIN || error==ENOBUFS)++t->socket_send_eagain;
        }
        errno=error;
    }
    return result;
}
}
