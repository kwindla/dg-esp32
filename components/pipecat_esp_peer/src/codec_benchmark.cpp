#include "pipecat/esp_peer/codec_benchmark.h"
#include "opus.h"
#include "esp_cpu.h"
#include "esp_heap_caps.h"
#include "esp_private/esp_clk.h"
#include "pthread_config.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <memory>
#include <numeric>
#include <stdexcept>
#include <thread>
#include <vector>
extern "C" {
void pipecat_codec_benchmark_begin(int internal);
void pipecat_codec_benchmark_end(void);
void pipecat_codec_benchmark_allocations(unsigned*, size_t*, unsigned*, unsigned*);
}
namespace pipecat::esp_peer {
namespace {
constexpr unsigned frames=500, stack_bytes=49152;
// aiortc's installed libopus encoder, VOIP/96 kbps/48 kHz stereo, returned
// this exact payload for all 500 server-track zero frames. See codec_inputs.py.
constexpr uint8_t server_silence[]={0xfc,0xff,0xfe};
portMUX_TYPE measurement_mux=portMUX_INITIALIZER_UNLOCKED;
struct AllocationScope {
    explicit AllocationScope(bool internal) { pipecat_codec_benchmark_begin(internal); }
    ~AllocationScope() { pipecat_codec_benchmark_end(); }
};
template<class F> unsigned measured(F operation) {
    // A single codec call is bounded to a few ms. IRQs and task switches on
    // this core are excluded; scheduling, fixture copies and logging are not
    // included in the cycle counter interval. Yield between every frame.
    portENTER_CRITICAL(&measurement_mux);
    auto start=esp_cpu_get_cycle_count();
    try {
        operation();
        auto elapsed=esp_cpu_get_cycle_count()-start;
        portEXIT_CRITICAL(&measurement_mux);
        return elapsed;
    } catch (...) { portEXIT_CRITICAL(&measurement_mux); throw; }
}
template<class T> using Buffer=std::unique_ptr<T,decltype(&std::free)>;
template<class T> Buffer<T> buffer(size_t count) {
    auto* ptr=static_cast<T*>(heap_caps_calloc(count,sizeof(T),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if (!ptr) throw std::bad_alloc();
    return Buffer<T>(ptr,&std::free);
}
nlohmann::json heap() {
    return {{"internal",heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)},
            {"psram",heap_caps_get_free_size(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT)}};
}
nlohmann::json allocations() {
    unsigned count,internal,external;size_t bytes;
    pipecat_codec_benchmark_allocations(&count,&bytes,&internal,&external);
    return {{"count",count},{"requested_bytes",bytes},{"internal",internal},{"external",external}};
}
nlohmann::json timings(const unsigned* cycles,unsigned hz,unsigned overhead) {
    std::vector<unsigned> raw(cycles,cycles+frames),sorted=raw;
    std::sort(sorted.begin(),sorted.end());
    auto us=[&](double v){return std::max(0.0,v-overhead)*1e6/hz;};
    return {{"frames",frames},{"raw_cycles",raw},{"mean_cpu_us",us(double(std::accumulate(raw.begin(),raw.end(),uint64_t(0)))/frames)},
        {"p50_cpu_us",us((double(sorted[249])+sorted[250])/2)}, {"p95_cpu_us",us(sorted[474])},
        {"min_cpu_us",us(sorted.front())},{"max_cpu_us",us(sorted.back())}};
}
void mode(nlohmann::json& counts,const uint8_t* data) {
    unsigned config=data[0]>>3;
    const char* name=config<12?"silk":config<16?"hybrid":"celt";
    counts[name]=counts.value(name,0u)+1;
}
nlohmann::json run_case(bool internal,bool fec,const int16_t* speech,size_t samples,unsigned hz,unsigned overhead) {
    nlohmann::json result={{"state",internal?"internal":"psram"},{"fec",fec},{"passed",false}};
    auto packets=buffer<uint8_t>(frames*1500);
    auto lengths=buffer<unsigned>(frames);
    auto cycles=buffer<unsigned>(frames*4);
    int16_t pcm[320],decoded[320];uint8_t packet[1500];
    try {
        AllocationScope scope(internal);
        nlohmann::json modes=nlohmann::json::object();
        result["speech_encoder_before"]=heap();
        {
            OpusEncoder encoder(24000,0,fec,false);
            result["speech_encoder_open"]=heap();
            for (unsigned frame=0;frame<frames;++frame) {
                for(unsigned i=0;i<320;++i)pcm[i]=speech[(frame*320+i)%samples];
                size_t bytes=0;
                cycles.get()[frame]=measured([&]{bytes=encoder.encode(pcm,packet,sizeof(packet));});
                if (!bytes || bytes>sizeof(packet))throw std::runtime_error("Benchmark packet length");
                lengths.get()[frame]=bytes;std::memcpy(packets.get()+frame*1500,packet,bytes);
                mode(modes,packet);vTaskDelay(1);
            }
        }
        result["speech_modes"]=modes;
        result["speech_encoder_allocations"]=allocations();
        result["speech_decoder_before"]=heap();
        {
            OpusDecoder decoder;
            result["speech_decoder_open"]=heap();
            for(unsigned frame=0;frame<frames;++frame) {
                std::memcpy(packet,packets.get()+frame*1500,lengths.get()[frame]);
                cycles.get()[frames+frame]=measured([&]{decoder.decode(packet,lengths.get()[frame],decoded,320,false);});
                vTaskDelay(1);
            }
        }
        std::memset(pcm,0,sizeof(pcm));
        result["zero_encoder_before"]=heap();
        nlohmann::json zero_modes=nlohmann::json::object(),zero_sizes=nlohmann::json::object();
        {
            OpusEncoder encoder(24000,0,fec,false);
            result["zero_encoder_open"]=heap();
            for(unsigned frame=0;frame<frames;++frame) {
                size_t bytes=0;
                cycles.get()[frames*2+frame]=measured([&]{bytes=encoder.encode(pcm,packet,sizeof(packet));});
                mode(zero_modes,packet);auto key=std::to_string(bytes);zero_sizes[key]=zero_sizes.value(key,0u)+1;
                vTaskDelay(1);
            }
        }
        result["zero_modes"]=zero_modes;result["zero_payload_sizes"]=zero_sizes;
        result["silence_decoder_before"]=heap();unsigned nonzero=0;
        {
            OpusDecoder decoder;
            result["silence_decoder_open"]=heap();
            for(unsigned frame=0;frame<frames;++frame) {
                std::memcpy(packet,server_silence,sizeof(server_silence));
                cycles.get()[frames*3+frame]=measured([&]{decoder.decode(packet,sizeof(server_silence),decoded,320,false);});
                for(auto sample:decoded)nonzero+=sample!=0;
                vTaskDelay(1);
            }
        }
        result["native_allocations"]=allocations();
        result["silence_nonzero_samples"]=nonzero;
        result["passed"]=modes.value("silk",0u)+modes.value("hybrid",0u)>0 && nonzero==0;
    } catch(const std::exception& e) {result["error"]=e.what();}
    if(result["passed"].get<bool>()) {
        for(unsigned i=0;i<4;++i)result[std::array<const char*,4>{"speech_encode","speech_decode","zero_encode","server_silence_decode"}[i]]=timings(cycles.get()+frames*i,hz,overhead);
    }
    result["stack_free"]=uxTaskGetStackHighWaterMark(nullptr);
    return result;
}
}
nlohmann::json codec_benchmark(const int16_t* speech,size_t samples) {
    if(!speech || !samples)throw std::invalid_argument("Empty benchmark speech");
    nlohmann::json result;
    dg::PthreadConfig threads;
    auto cfg=esp_pthread_get_default_config();cfg.thread_name="codec-bench";cfg.stack_size=stack_bytes;
    cfg.prio=24;cfg.pin_to_core=1;cfg.stack_alloc_caps=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT;
    threads.set(cfg);
    try {
        std::thread worker([&]{
            try {
                unsigned hz=esp_clk_cpu_freq(),overhead=UINT32_MAX;
                for(unsigned i=0;i<100;++i)overhead=std::min(overhead,measured([]{}));
                result={{"cpu_hz",hz},{"core",1},{"priority",24},{"stack_bytes",stack_bytes},
                    {"stack_internal",true},{"irq_masked_per_call",true},{"clock_overhead_cycles",overhead},
                    {"frames_per_measurement",frames},{"frame_samples",320},{"sample_rate",16000},
                    {"bitrate",24000},{"complexity",0},{"dtx",false},{"server_silence_hex","fcfffe"},
                    {"server_silence_bytes",3},{"decoder_recovery","normal; FEC flag belongs to corresponding encoder"},
                    {"cases",nlohmann::json::array()}};
                for(bool internal:{false,true})for(bool fec:{false,true})result["cases"].push_back(run_case(internal,fec,speech,samples,hz,overhead));
            } catch(const std::exception& e) {result["error"]=e.what();}
        });
        worker.join();
    } catch(...) {throw;}
    return result;
}
nlohmann::json silence_resume_benchmark(const uint8_t* stream,size_t bytes) {
    if(bytes<8 || std::memcmp(stream,"DSIL",4))throw std::invalid_argument("Silence fixture header");
    nlohmann::json result;
    dg::PthreadConfig threads;
    auto cfg=esp_pthread_get_default_config();cfg.thread_name="silence-bench";cfg.stack_size=stack_bytes;
    cfg.prio=24;cfg.pin_to_core=1;cfg.stack_alloc_caps=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT;
    threads.set(cfg);
    try {
        std::thread worker([&] {
            try {
                AllocationScope scope(false);OpusDecoder full,skipped;
                uint8_t packet[1500];int16_t a[320],b[320];
                static constexpr uint8_t silence[]={0x48,0x07,0xc9,0x79,0xc8,0xc9,0x57,0xc0};
                auto u32=[](const uint8_t* p){return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;};
                unsigned count=u32(stream+4),skip_count=0,resumes=0,max_resume_delta=0,max_silence=0;
                size_t offset=8;bool was_silent=false;unsigned resume_window=0;
                result={{"frames",nlohmann::json::array()},{"cpu_hz",esp_clk_cpu_freq()},{"stack_bytes",stack_bytes},
                    {"recognition","exact 4807c979c8c957c0 payload; no size-only heuristic"}};
                for(unsigned i=0;i<count;++i) {
                    if(offset+2>bytes)throw std::runtime_error("Short fixture");
                    size_t n=stream[offset]|size_t(stream[offset+1])<<8;offset+=2;
                    if(!n || n>sizeof(packet) || offset+n>bytes)throw std::runtime_error("Invalid fixture packet");
                    std::memcpy(packet,stream+offset,n);offset+=n;
                    bool quiet=n==sizeof(silence) && !std::memcmp(packet,silence,n);
                    unsigned full_cycles=measured([&]{full.decode(packet,n,a,320,false);});
                    unsigned skip_cycles=0;
                    if(quiet) {std::fill_n(b,320,0);++skip_count;}
                    else skip_cycles=measured([&]{skipped.decode(packet,n,b,320,false);});
                    if(!quiet && was_silent) {++resumes;resume_window=5;}
                    unsigned delta=0,peak=0;
                    for(unsigned j=0;j<320;++j) {
                        delta=std::max(delta,unsigned(std::abs(int(a[j])-b[j])));
                        peak=std::max(peak,unsigned(std::abs(int(a[j]))));
                    }
                    if(quiet)max_silence=std::max(max_silence,peak);
                    if(resume_window) {max_resume_delta=std::max(max_resume_delta,delta);--resume_window;}
                    result["frames"].push_back({{"index",i},{"silence",quiet},{"full_cycles",full_cycles},
                        {"skip_cycles",skip_cycles},{"max_sample_delta",delta},{"full_peak",peak}});
                    was_silent=quiet;vTaskDelay(1);
                }
                if(offset!=bytes)throw std::runtime_error("Trailing fixture bytes");
                result["skipped_frames"]=skip_count;result["resumes"]=resumes;
                result["max_resume_delta_lsb"]=max_resume_delta;result["max_silence_peak_lsb"]=max_silence;
                result["state_preserved"]=max_resume_delta<=1;
                result["stack_free_bytes"]=uxTaskGetStackHighWaterMark(nullptr);
            } catch(const std::exception& e) {result["error"]=e.what();}
        });
        worker.join();
    } catch(...) {throw;}
    return result;
}
}
