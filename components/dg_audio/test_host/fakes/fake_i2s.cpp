#include "fake_i2s.h"
#include "tx_ring.h"
#include "driver/i2s_std.h"
#include "dg_audio/mic.h"
#include "dg_audio/aec.h"
#include <algorithm>
#include <thread>
#include <chrono>
struct i2s_channel_obj_t {i2s_event_callbacks_t callbacks;void* context=nullptr;bool enabled=false;uint64_t completed=0;int64_t start=0;TxRing ring;};
namespace fake {
std::atomic<bool> mic_samples{false};
bool ring_enabled=false;
bool aec_enabled=true;unsigned aec_gain=0;
std::mutex mutex;std::atomic<int64_t> now{0};std::vector<int16_t> submitted,submitted_stereo;std::vector<unsigned> chunks;
std::function<void(unsigned)> on_write;unsigned maximum_write=320,failures=0,writes=0;
i2s_channel_obj_t* channel=nullptr;
void reset(bool use_ring) {std::lock_guard<std::mutex> lock(mutex);now=0;ring_enabled=use_ring;mic_samples=false;aec_enabled=true;aec_gain=0;submitted.clear();submitted_stereo.clear();chunks.clear();on_write={};maximum_write=320;failures=writes=0;}
void advance(unsigned ms) {
    std::lock_guard<std::mutex> lock(mutex);now+=ms*1000;
    if(!channel || !channel->enabled)return;
    auto target=uint64_t((now-channel->start)*16/1000)/240*240;
    while(channel->completed<target) {
        channel->completed+=240;
        if(ring_enabled)channel->ring.complete();
        i2s_event_data_t event{960};
        if(channel->callbacks.on_sent)channel->callbacks.on_sent(channel,&event,channel->context);
    }
}
}
int64_t esp_timer_get_time() {return fake::now;}
void vTaskDelay(unsigned ms) {fake::advance(std::max(1u,ms));std::this_thread::sleep_for(std::chrono::microseconds(100));}
int i2s_new_channel(const i2s_chan_config_t*,i2s_chan_handle_t* out,i2s_chan_handle_t*) {*out=new i2s_channel_obj_t;fake::channel=*out;return 0;}
int i2s_channel_init_std_mode(i2s_chan_handle_t,const i2s_std_config_t*) {return 0;}
int i2s_channel_register_event_callback(i2s_chan_handle_t c,const i2s_event_callbacks_t* cb,void* context) {c->callbacks=*cb;c->context=context;return 0;}
int i2s_channel_enable(i2s_chan_handle_t c) {c->enabled=true;c->completed=0;c->start=fake::now;return 0;}
int i2s_channel_disable(i2s_chan_handle_t c) {c->enabled=false;c->ring.reset();return 0;}
int i2s_del_channel(i2s_chan_handle_t c) {fake::channel=nullptr;delete c;return 0;}
int i2s_channel_preload_data(i2s_chan_handle_t c,const void*,size_t bytes,size_t* loaded) {*loaded=fake::ring_enabled?c->ring.preload(bytes/4)*4:bytes;return 0;}
int i2s_channel_write(i2s_chan_handle_t c,const void* data,size_t bytes,size_t* written,unsigned timeout_ms) {
    bool timed_out=false;
    unsigned accepted=0;
    if(fake::ring_enabled) {
        const unsigned requested=std::min(unsigned(bytes/4),fake::maximum_write);
        while(accepted<requested) {
            unsigned n;
            {std::lock_guard<std::mutex> lock(fake::mutex);n=c->ring.write(requested-accepted);}
            if(n) {accepted+=n;continue;}
            auto remaining_us=15000-unsigned((fake::now-c->start)%15000);
            if(remaining_us>timeout_ms*1000) {fake::advance(timeout_ms);timed_out=true;break;}
            fake::advance((remaining_us+999)/1000);
        }
    }
    std::function<void(unsigned)> callback;unsigned number;bool failure;
    {std::lock_guard<std::mutex> lock(fake::mutex);
    number=++fake::writes;failure=fake::failures>0;if(failure)--fake::failures;
    unsigned n=failure?0:(fake::ring_enabled?accepted:std::min(unsigned(bytes/4),fake::maximum_write));
    const auto* stereo=static_cast<const int16_t*>(data);
    for(unsigned i=0;i<n;++i)fake::submitted.push_back(stereo[2*i]);
    fake::submitted_stereo.insert(fake::submitted_stereo.end(),stereo,stereo+2*n);
    fake::chunks.push_back(n);*written=n*4;callback=fake::on_write;}
    // Inject DMA completions and invalidation before the write returns. The
    // driver accepted only the prefix above; its source memory isn't retained.
    if(callback)callback(number);
    if(failure)fake::advance(1);
    return timed_out?ESP_ERR_TIMEOUT:(failure?-1:0);
}
namespace dg_audio {
AecStatus aec_status() {AecStatus s;s.enabled=fake::aec_enabled;s.reference_gain_db=fake::aec_gain;return s;}
Mic::Mic():rx_clock_(240,4){};Mic::~Mic()=default;
size_t Mic::read(int16_t* pcm,size_t n,unsigned,int16_t*) {
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
    if(!fake::mic_samples)return 0;
    std::fill_n(pcm,n,int16_t(30000));clips_+=3;return n;
}
void Mic::gain_percent(unsigned n) {gain_percent_=n;}
}
void mic_report(const int16_t*,float*,float*,float*) {}
