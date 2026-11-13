// Disconnected step-1b measurements. Production sources remain untouched.
#include "audio_b.h"
#include "measure.h"
#include "dg_audio/reference_gain.h"
#include "esp_afe_aec.h"
#include "esp_cpu.h"
#include "esp_private/esp_clk.h"
#include "esp_heap_caps.h"
#include "esp_peer_default.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"
#include "mbedtls/base64.h"
#if CONFIG_P4_PROBE_OPUS
#include "opus.h"
#endif
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstring>
#include <vector>
#if !CONFIG_HEAP_ABORT_WHEN_ALLOCATION_FAILS || CONFIG_PM_SLEEP_CLK_ICG_ENABLE
#error "Regenerate spike config: heap fail-stop required, unused sleep-clock ICG disabled"
#endif

namespace probe_b {
namespace {
using measure::emit;
constexpr unsigned rate=16000, internal=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT, external=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;
i2c_master_bus_handle_t bus;
i2s_chan_handle_t rx,tx;
const audio_codec_if_t *adc,*dac;
bool audio_ready=false;unsigned selected_refgain=15,selected_refreg=0x15;
constexpr unsigned aec_stack=16384,opus_stack=32768;
std::atomic<unsigned> overflows{0};
std::atomic<bool> touch_busy{false};
bool overflow(i2s_chan_handle_t, i2s_event_data_t*, void*) { ++overflows;return false; }
void* buffer(size_t bytes) {
    auto* p=heap_caps_aligned_calloc(16,1,bytes,external);
    ESP_ERROR_CHECK(p?ESP_OK:ESP_ERR_NO_MEM);return p;
}
void heap(const char* phase) {
    emit("{\"kind\":\"heap\",\"phase\":\"%s\",\"internal_free\":%u,\"internal_largest\":%u,\"internal_min\":%u,\"psram_free\":%u,\"psram_largest\":%u}",phase,
        unsigned(heap_caps_get_free_size(internal)),unsigned(heap_caps_get_largest_free_block(internal)),
        unsigned(heap_caps_get_minimum_free_size(internal)),unsigned(heap_caps_get_free_size(external)),unsigned(heap_caps_get_largest_free_block(external)));
}
void drain() {
    uint32_t data[320];size_t bytes;
    // Empty only the bounded stale DMA queue, then measure future frames.
    for(unsigned i=0;i<32;++i) if(i2s_channel_read(rx,data,sizeof data,&bytes,0)!=ESP_OK) break;
}
struct Stats {
    unsigned n=0,clips=0,peak=0;double sum=0,power=0;
    void add(int16_t value) { ++n;sum+=value;power+=double(value)*value;peak=std::max(peak,unsigned(std::abs(int(value))));clips+=value==32767 || value==-32768; }
    double db() const { return 10*log10(std::max(0.0,power/std::max(1u,n)-pow(sum/std::max(1u,n),2))/(32768.0*32768.0)+1e-15); }
};
std::vector<int16_t> playback;
unsigned pcm_expected=0,pcm_seq=0;bool pcm_ready=false;
struct Stimulus { unsigned ms,mode;std::atomic<bool> done{false}; };
void play(void* arg) {
    auto& s=*static_cast<Stimulus*>(arg);int16_t data[320];unsigned at=0,total=rate*s.ms/1000;
    double phase=0;
    while(at<total) {
        unsigned n=std::min(160u,total-at);
        for(unsigned i=0;i<n;++i,++at) {
            double hz=s.mode==1?300+2700.0*at/total:1000;
            phase+=2*M_PI*hz/rate;
            auto sample=s.mode==2?playback[at%playback.size()]:int16_t(32767*pow(10,-12.0/20)*sin(phase));
            data[2*i]=data[2*i+1]=sample;
        }
        size_t written=0;ESP_ERROR_CHECK(i2s_channel_write(tx,data,n*4,&written,1000));
        ESP_ERROR_CHECK(written==n*4?ESP_OK:ESP_FAIL);
    }
    s.done=true;vTaskDelete(nullptr);
}
void start(Stimulus& s) {
    ESP_ERROR_CHECK(xTaskCreatePinnedToCore(play,"B-play",4096,&s,10,nullptr,0)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
}
void join(Stimulus& s) { while(!s.done)vTaskDelay(1); }
void refgain(unsigned db) {
    selected_refgain=db;
    ESP_ERROR_CHECK(adc->set_reg(adc,0x45,dg_audio::reference_gain_register(db)));
    int got=-1;ESP_ERROR_CHECK(adc->get_reg(adc,0x45,&got));
    ESP_ERROR_CHECK(got==int(dg_audio::reference_gain_register(db))?ESP_OK:ESP_FAIL);
    selected_refreg=unsigned(got);
    emit("{\"kind\":\"refgain\",\"db\":%u,\"reg\":%d}",db,got);
}
void slots(unsigned mode,unsigned ms,bool sine=false) {
    // PA off isolates the DAC electrical stimulus from the speaker's acoustic
    // path. Mode 2 records an optional external/Jabra stimulus with DAC muted.
    ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_53,0));ESP_ERROR_CHECK(dac->mute(dac,mode!=1));
    drain();auto before=overflows.load();std::array<Stats,4> stats{};
    Stimulus s{ms,sine?0u:1u};if(mode==1)start(s);
    uint32_t data[320];unsigned frames=0,target=rate*ms/1000;
    while(frames<target) {
        size_t bytes=0;ESP_ERROR_CHECK(i2s_channel_read(rx,data,sizeof data,&bytes,1000));
        ESP_ERROR_CHECK(bytes && bytes%8==0?ESP_OK:ESP_FAIL);
        for(unsigned i=0;i<bytes/8 && frames<target;++i,++frames) {
            stats[0].add(int16_t(data[2*i]>>16));stats[1].add(int16_t(data[2*i+1]>>16));
            stats[2].add(int16_t(data[2*i]));stats[3].add(int16_t(data[2*i+1]));
        }
    }
    auto lost=overflows.load()-before;if(mode==1)join(s);
    for(unsigned ch=0;ch<4;++ch) {
        int gain=-1;ESP_ERROR_CHECK(adc->get_reg(adc,0x43+ch,&gain));auto& st=stats[ch];
        emit("{\"kind\":\"slot\",\"mode\":%u,\"channel\":%u,\"samples\":%u,\"rms_dbfs\":%.6f,\"peak\":%u,\"clips\":%u,\"gain_reg\":%d}",mode,ch+1,st.n,st.db(),st.peak,st.clips,gain);
    }
    emit("{\"kind\":\"slots_end\",\"frames\":%u,\"overflows\":%u}",frames,lost);
    ESP_ERROR_CHECK(dac->mute(dac,false));ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_53,1));
}
unsigned aec_seconds,aec_stimulus;std::atomic<bool> aec_done{false};
void aec_worker(void*) {
    ulTaskNotifyTake(pdTRUE,portMAX_DELAY); // Parent snapshots stack/TCB before AFE creation.
    heap("before_create");auto* h=afe_aec_create("MR",4,AFE_TYPE_FD,AFE_MODE_LOW_COST);
    // Match dg_audio/src/aec.cpp's fail-closed validation, including nested
    // factory state. Never destroy an incomplete vendor factory result.
    if(!h || !h->handle || !h->handle->aec_handle || !h->data || !h->pcm_config.mic_ids || !h->pcm_config.ref_ids) std::abort();
    const auto& p=h->pcm_config;
    ESP_ERROR_CHECK(p.total_ch_num==2 && p.mic_num==1 && p.ref_num==1 && p.sample_rate==16000 &&
        p.mic_ids[0]==0 && p.ref_ids[0]==1 && h->mode==AEC_MODE_FD_LOW_COST?ESP_OK:ESP_FAIL);
    int chunk=afe_aec_get_chunksize(h);ESP_ERROR_CHECK(chunk>0 && chunk<=2048?ESP_OK:ESP_FAIL);
    heap("after_create");
    auto* raw=static_cast<uint32_t*>(buffer(chunk*8));auto* in=static_cast<int16_t*>(buffer(chunk*4));
    auto* out=static_cast<int16_t*>(buffer(chunk*2));heap("after_buffers");
    emit("{\"kind\":\"aec_begin\",\"chunk\":%d,\"rate\":16000,\"channels\":2,\"mic\":1,\"ref\":3,\"core\":%d,\"seconds\":%u,\"stimulus\":%u,\"cpu_hz\":%d,\"ref_gain_db\":%u,\"ref_gain_reg\":%u,\"dma_descriptors\":4,\"dma_frames\":240,\"stack_bytes\":%u,\"stack_memory\":\"PSRAM\"}",chunk,xPortGetCoreID(),aec_seconds,aec_stimulus,esp_clk_cpu_freq(),selected_refgain,selected_refreg,aec_stack);
    ESP_ERROR_CHECK(dac->mute(dac,false));ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_53,1));
    drain();Stimulus stimulus{aec_seconds*1000,aec_stimulus==2?2u:0u};start(stimulus);
    unsigned initial_overflows=overflows.load(),seq=0;uint64_t samples=0,total_cycles=0,total_us=0,ref_clips=0,out_clips=0;
    unsigned max_cycles=0,max_us=0;double mic_power=0,out_power=0;
    uint64_t target=uint64_t(rate)*aec_seconds;
    auto began=esp_timer_get_time();
    while(samples<target) {
        size_t bytes=0;
        while(bytes<size_t(chunk*8)) {
            size_t got=0;ESP_ERROR_CHECK(i2s_channel_read(rx,reinterpret_cast<uint8_t*>(raw)+bytes,chunk*8-bytes,&got,1000));
            ESP_ERROR_CHECK(got && got%8==0?ESP_OK:ESP_FAIL);bytes+=got;
        }
        for(int i=0;i<chunk;++i) { in[2*i]=int16_t(raw[2*i]>>16);in[2*i+1]=int16_t(raw[2*i]); }
        auto t=esp_timer_get_time();auto c=esp_cpu_get_cycle_count();
        size_t produced=afe_aec_process(h,in,out);
        unsigned cycles=esp_cpu_get_cycle_count()-c,us=esp_timer_get_time()-t;
        ESP_ERROR_CHECK(produced==size_t(chunk*2)?ESP_OK:ESP_FAIL);
        double mp=0,op=0,rp=0;
        for(int i=0;i<chunk;++i) {
            mp+=double(in[2*i])*in[2*i];rp+=double(in[2*i+1])*in[2*i+1];op+=double(out[i])*out[i];
            ref_clips+=in[2*i+1]==32767 || in[2*i+1]==-32768;out_clips+=out[i]==32767 || out[i]==-32768;
        }
        // Exclude initial adaptation from the crude far-end tone power ratio.
        if(samples>=rate*2) { mic_power+=mp;out_power+=op; }
        emit("{\"kind\":\"aec_chunk\",\"seq\":%u,\"cycles\":%u,\"us\":%u,\"core\":%d,\"bytes\":%u,\"mic_power\":%.0f,\"ref_power\":%.0f,\"out_power\":%.0f}",seq++,cycles,us,xPortGetCoreID(),unsigned(produced),mp,rp,op);
        samples+=chunk;total_cycles+=cycles;total_us+=us;max_cycles=std::max(max_cycles,cycles);max_us=std::max(max_us,us);
    }
    unsigned lost=overflows.load()-initial_overflows;auto elapsed=esp_timer_get_time()-began;join(stimulus);
    emit("{\"kind\":\"aec_end\",\"chunks\":%u,\"samples\":%llu,\"output_samples\":%llu,\"elapsed_us\":%lld,\"cycles\":%llu,\"max_cycles\":%u,\"process_us\":%llu,\"max_us\":%u,\"overflows\":%u,\"reference_clips\":%llu,\"output_clips\":%llu,\"erle_db\":%.6f}",
        seq,(unsigned long long)samples,(unsigned long long)samples,(long long)elapsed,(unsigned long long)total_cycles,max_cycles,(unsigned long long)total_us,max_us,lost,
        (unsigned long long)ref_clips,(unsigned long long)out_clips,10*log10((mic_power+1)/(out_power+1)));
    heap("end");free(raw);free(in);free(out);afe_aec_destroy(h);heap("after_destroy");aec_done=true;vTaskSuspend(nullptr);
}

struct TouchRun { unsigned seconds;bool codec;std::atomic<bool> done{false},codec_done{false};unsigned ops=0,errors=0; } touch_run;
void codec_traffic(void*) {
    auto tick=xTaskGetTickCount();
    while(!touch_run.done) {
        int value;auto a=adc->get_reg(adc,0x43,&value);auto b=dac->get_reg(dac,0x44,&value);
        touch_run.ops+=2;touch_run.errors+=(a!=ESP_OK)+(b!=ESP_OK);vTaskDelayUntil(&tick,pdMS_TO_TICKS(20));
    }
    touch_run.codec_done=true;vTaskDelete(nullptr);
}
esp_err_t touch_read(i2c_master_dev_handle_t dev,unsigned reg,void* data,size_t n) {
    uint8_t address[]={uint8_t(reg>>8),uint8_t(reg)};
    return i2c_master_transmit_receive(dev,address,2,static_cast<uint8_t*>(data),n,50);
}
void touch_worker(void*) {
    // GPIO27 is shared LCD/touch reset; panel reset can relatch the address.
    i2c_device_config_t config{};config.dev_addr_length=I2C_ADDR_BIT_LEN_7;config.scl_speed_hz=400000;
    i2c_master_dev_handle_t dev=nullptr;uint8_t size[4]{},product[4]{};esp_err_t rc=ESP_ERR_NOT_FOUND;
    unsigned address=0;
    for(unsigned candidate:{0x5du,0x14u}) {
        config.device_address=candidate;
        rc=bus?i2c_master_probe(bus,candidate,50):ESP_ERR_INVALID_STATE;
        if(rc!=ESP_OK)continue;
        rc=i2c_master_bus_add_device(bus,&config,&dev);
        if(rc!=ESP_OK)continue;
        rc=touch_read(dev,0x8048,size,4);
        if(rc==ESP_OK)rc=touch_read(dev,0x8140,product,4);
        if(rc==ESP_OK){address=candidate;break;}
        i2c_master_bus_rm_device(dev);dev=nullptr;
    }
    if(!address) {
        emit("{\"kind\":\"touch_error\",\"reason\":\"GT911 unavailable\",\"tried\":[93,20],\"rc\":%d}",rc);
        touch_run.done=true;touch_busy=false;vTaskDelete(nullptr);return;
    }
    if(touch_run.codec)ESP_ERROR_CHECK(xTaskCreate(codec_traffic,"B-codec-io",4096,nullptr,5,nullptr)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
    unsigned xmax=size[0]|size[1]<<8,ymax=size[2]|size[3]<<8;
    emit("{\"kind\":\"touch_begin\",\"address\":%u,\"product_id_hex\":\"%02x%02x%02x%02x\",\"x_max\":%u,\"y_max\":%u,\"hz\":50,\"codec_traffic\":%s}",
        address,product[0],product[1],product[2],product[3],xmax,ymax,touch_run.codec?"true":"false");
    auto tick=xTaskGetTickCount();unsigned errors=0,events=0;
    for(unsigned seq=0;seq<touch_run.seconds*50;++seq) {
        auto t=esp_timer_get_time();uint8_t status=0,data[40]{};auto rc=touch_read(dev,0x814e,&status,1);
        unsigned count=status&15;
        if(rc==ESP_OK && status&0x80) {
            if(count>5) rc=ESP_ERR_INVALID_RESPONSE;
            else if(count) rc=touch_read(dev,0x814f,data,count*8);
            if(rc==ESP_OK) {uint8_t clear[]={0x81,0x4e,0};rc=i2c_master_transmit(dev,clear,3,50);}
        }
        auto us=esp_timer_get_time()-t;errors+=rc!=ESP_OK;
        // Report bus latency separately from time spent emitting UART records.
        if(rc==ESP_OK && status&0x80) for(unsigned i=0;i<count;++i) {
            unsigned x=data[8*i+1]|data[8*i+2]<<8,y=data[8*i+3]|data[8*i+4]<<8;
            probe::print("TOUCH %u %u\n",x,y);++events;
        }
        emit("{\"kind\":\"touch_poll\",\"seq\":%u,\"us\":%lld,\"rc\":%d,\"started_us\":%lld}",seq,(long long)us,rc,(long long)t);
        vTaskDelayUntil(&tick,pdMS_TO_TICKS(20));
    }
    auto remove_rc=i2c_master_bus_rm_device(dev);errors+=remove_rc!=ESP_OK;touch_run.done=true;
    if(touch_run.codec) while(!touch_run.codec_done)vTaskDelay(1);
    emit("{\"kind\":\"touch_end\",\"polls\":%u,\"events\":%u,\"errors\":%u,\"codec_ops\":%u,\"codec_errors\":%u}",touch_run.seconds*50,events,errors,touch_run.ops,touch_run.errors);
    touch_busy=false;vTaskDelete(nullptr);
}
unsigned opus_frames;std::atomic<bool> opus_done{false};
void opus_worker(void*) {
#if CONFIG_P4_PROBE_OPUS
    { // Destroy codec objects before the parent deletes the suspended worker.
    pipecat::esp_peer::OpusEncoder enc(24000,0,false,false,20);
    pipecat::esp_peer::OpusDecoder dec;dec.reset();
    emit("{\"kind\":\"opus_begin\",\"codec_version\":\"2.5.0\",\"rate\":16000,\"channels\":1,\"frame_ms\":20,\"pcm_bytes\":640,\"bitrate\":24000,\"complexity\":0,\"fec\":false,\"dtx\":false,\"stack_bytes\":%u,\"stack_memory\":\"INTERNAL\",\"core\":%d,\"input\":\"%s\"}",opus_stack,xPortGetCoreID(),pcm_ready?"uploaded_speech":"synthetic_sine");
    int16_t pcm[320],decoded[320];uint8_t packet[1276];
    for(unsigned seq=0;seq<opus_frames;++seq) {
        for(unsigned i=0;i<320;++i)pcm[i]=pcm_ready?playback[(seq*320+i)%playback.size()]:int16_t(4000*sin(2*M_PI*1000*(seq*320+i)/rate));
        auto c=esp_cpu_get_cycle_count();auto encoded=enc.encode(pcm,packet,sizeof packet);auto ec=esp_cpu_get_cycle_count()-c;
        c=esp_cpu_get_cycle_count();auto samples=dec.decode(packet,encoded,decoded,320,false);auto dcycles=esp_cpu_get_cycle_count()-c;
        // Exercise production's zero-length PLC path and its non-null placeholder quirk.
        c=esp_cpu_get_cycle_count();auto concealed=dec.decode(nullptr,0,decoded,320,true);auto pc=esp_cpu_get_cycle_count()-c;
        ESP_ERROR_CHECK(samples==320 && concealed==320?ESP_OK:ESP_FAIL);
        emit("{\"kind\":\"opus_frame\",\"seq\":%u,\"encode_cycles\":%u,\"decode_cycles\":%u,\"plc_cycles\":%u,\"plc_samples\":%u,\"encoded_bytes\":%u,\"decoded_samples\":%u,\"core\":%d}",seq,unsigned(ec),unsigned(dcycles),unsigned(pc),unsigned(concealed),unsigned(encoded),unsigned(samples),xPortGetCoreID());
        vTaskDelay(1);
    }
    } // Includes codec destruction in high-water witness. IDF reports bytes.
    emit("{\"kind\":\"opus_end\",\"frames\":%u,\"plc_frames\":%u,\"stack_high_water_bytes\":%u}",opus_frames,opus_frames,unsigned(uxTaskGetStackHighWaterMark(nullptr)));
#else
    emit("{\"kind\":\"opus_unavailable\",\"reason\":\"P4_PROBE_OPUS disabled in this build\"}");
#endif
    opus_done=true;vTaskSuspend(nullptr);
}
// Workers finish their destructors then park. Delete only after suspension so
// heap snapshots exclude both their stack and internal TCB deterministically.
void reap(TaskHandle_t worker,std::atomic<bool>& done) {
    while(!done || eTaskGetState(worker)!=eSuspended)vTaskDelay(1);
    vTaskDeleteWithCaps(worker);
}

}
esp_err_t register_rx(i2s_chan_handle_t channel) {
    i2s_event_callbacks_t cb{};cb.on_recv_q_ovf=overflow;return i2s_channel_register_event_callback(channel,&cb,nullptr);
}
void init(i2c_master_bus_handle_t b,i2s_chan_handle_t r,i2s_chan_handle_t t,const audio_codec_if_t* a,const audio_codec_if_t* d,bool ready) {
    audio_ready=ready && b && r && t && a && d;
    if(!audio_ready)emit("{\"kind\":\"codec_unavailable\",\"console_usable\":true}");
    bus=b;rx=r;tx=t;adc=a;dac=d;
}
bool command(const char* line,bool loop_busy) {
    char name[20]{},first[20]{},second[20]{};int a=0,b=0;char tail;
    int n=sscanf(line,"%19s %19s %19s %c",name,first,second,&tail);
    if(!strcmp(name,"pcm_data")) {
        unsigned seq,crc;char encoded[700];
        if(sscanf(line,"%*s %u %x %699s %c",&seq,&crc,encoded,&tail)!=3 || !pcm_expected || pcm_ready || seq!=pcm_seq) {
            probe::print("ERR PCM sequence\n");return true;
        }
        uint8_t raw[512];size_t bytes=0;
        if(mbedtls_base64_decode(raw,sizeof raw,&bytes,reinterpret_cast<unsigned char*>(encoded),strlen(encoded)) || bytes%2 ||
           probe::crc32(raw,bytes)!=crc || playback.size()+bytes/2>pcm_expected) {probe::print("ERR PCM data\n");return true;}
        for(size_t i=0;i<bytes;i+=2)playback.push_back(int16_t(raw[i]|raw[i+1]<<8));
        ++pcm_seq;return true;
    }
    if(strcmp(name,"slots") && strcmp(name,"refgain") && strcmp(name,"level") && strcmp(name,"aec") &&
       strcmp(name,"opus") && strcmp(name,"peer") && strcmp(name,"touch") && strcmp(name,"pcm_begin") && strcmp(name,"pcm_end"))return false;
    for(int i=0;i<n-1 && i<2;++i) {
        char* end=nullptr;auto v=strtol(i?second:first,&end,10);
        if(!end || *end || v < -1000000 || v > 1000000) {probe::print("ERR B integer arguments\n");return true;}
        (i?b:a)=int(v);
    }
    if(!audio_ready && (!strcmp(name,"slots") || !strcmp(name,"refgain") || !strcmp(name,"level") ||
        !strcmp(name,"aec") || (!strcmp(name,"touch") && b))) {probe::print("ERR B codec unavailable\n");return true;}
    if(n==4 || (loop_busy && strcmp(name,"touch"))) {probe::print("ERR B busy or arguments\n");return true;}
    if(!strcmp(name,"slots") && n==3 && a>=0 && a<=2 && b>=100 && b<=5000)slots(a,b);
    else if(!strcmp(name,"refgain") && n==2 && dg_audio::valid_reference_gain(a))refgain(a);
    else if(!strcmp(name,"level") && n==3 && (a==-20 || a==-12 || a==-6 || a==0) && dg_audio::valid_reference_gain(b)) {
        ESP_ERROR_CHECK(dac->set_vol(dac,float(a)));refgain(b);slots(1,1000,true);
    }
    else if(!strcmp(name,"aec") && n==3 && a>=1 && a<=600 && (b==1 || (b==2 && pcm_ready))) {
        // Duplex production uses DG_SPEAKER_DMA_* (4x240), not standalone
        // DG_MIC_DMA_* (3x160): dg_audio/src/codec/i2s_duplex.cpp and Kconfig.
        if(!configure_dma(4,240,rx,tx)){audio_ready=false;probe::print("ERR B DMA setup\n");return true;}
        refgain(selected_refgain);aec_seconds=a;aec_stimulus=b;aec_done=false;
        heap("before_worker");TaskHandle_t worker=nullptr;
        ESP_ERROR_CHECK(xTaskCreatePinnedToCoreWithCaps(aec_worker,"B-AFE",aec_stack,nullptr,12,&worker,1,external)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
        heap("after_worker_create");xTaskNotifyGive(worker);reap(worker,aec_done);heap("after_worker_delete");
        if(!configure_dma(6,320,rx,tx)){audio_ready=false;probe::print("ERR B DMA restore\n");}
    }
    else if(!strcmp(name,"opus") && n==2 && a>=1 && a<=30000) {
        opus_frames=a;opus_done=false;TaskHandle_t worker=nullptr;
        ESP_ERROR_CHECK(xTaskCreatePinnedToCoreWithCaps(opus_worker,"B-Opus",opus_stack,nullptr,10,&worker,0,internal)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
        reap(worker,opus_done);
    }
    else if(!strcmp(name,"peer") && n==1) {
        auto* ops=esp_peer_get_default_impl();ESP_ERROR_CHECK(ops && ops->open && ops->close?ESP_OK:ESP_FAIL);
        emit("{\"kind\":\"peer\",\"version\":\"1.5.7\",\"default_ops_linked\":true}");
    }
    else if(!strcmp(name,"touch") && n==3 && a>=1 && a<=600 && (b==0 || b==1) && !touch_busy.exchange(true)) {
        touch_run.seconds=a;touch_run.codec=b;touch_run.done=false;touch_run.codec_done=false;touch_run.ops=touch_run.errors=0;
        ESP_ERROR_CHECK(xTaskCreate(touch_worker,"B-touch",4096,nullptr,5,nullptr)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
    }
    else if(!strcmp(name,"pcm_begin") && n==2 && a>=320 && a<=30*16000) {
        playback.clear();playback.reserve(a);pcm_expected=a;pcm_seq=0;pcm_ready=false;
    }
    else if(!strcmp(name,"pcm_end") && n==1 && pcm_expected && playback.size()==pcm_expected) {
        unsigned peak=0;double power=0;
        for(auto v:playback){peak=std::max(peak,unsigned(std::abs(int(v))));power+=double(v)*v;}
        // -12 dBFS relative to PCM16 full scale 32768, rounded down.
        if(peak>8230){pcm_ready=false;probe::print("ERR PCM peak exceeds -12 dBFS\n");return true;}
        pcm_ready=true;emit("{\"kind\":\"pcm_ready\",\"samples\":%u,\"peak\":%u,\"peak_dbfs\":%.6f,\"rms_dbfs\":%.6f}",pcm_expected,peak,20*log10(std::max(1e-15,peak/32768.0)),10*log10(std::max(1e-15,power/playback.size()/(32768.0*32768.0))));
    }
    else probe::print("ERR B arguments or busy\n");
    return true;
}
}
