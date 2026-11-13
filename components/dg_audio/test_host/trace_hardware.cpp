// Hardware boundary only. ES8311/ES7210 register programming is the real,
// pinned esp_codec_dev implementation, linked identically before and after.
#include "sdkconfig.h"
#include "dg_board/i2c.h"
#include "driver/i2s_std.h"
#include "driver/i2c_master.h"
#include "esp_codec_dev_defaults.h"
#include "codec_ref_mgr.h"
#include "dg_audio/aec.h"
#include "dg_audio/mic.h"
#include "dg_audio/speaker.h"
#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
namespace {
uint8_t registers[128][256]{};
int64_t now=0;
bool nack=false,final_readback=false;
int bad_register=-1;
const char* label(i2s_chan_handle_t);
}
struct i2s_channel_obj_t {
    int port;bool tx;int role;bool paired=false;
    i2s_std_config_t config{};i2s_event_callbacks_t callbacks{};
};
namespace {
i2s_channel_obj_t input{0,false,I2S_ROLE_MASTER},output{0,true,I2S_ROLE_MASTER};
const char* label(i2s_chan_handle_t p) {return p->tx?"TX":"RX";}
}
extern "C" void audio_trace_log(const char* tag,const char* format,...) {
    std::printf("LOG %s ",tag);va_list args;va_start(args,format);std::vprintf(format,args);va_end(args);std::puts("");
}
int64_t esp_timer_get_time() {return now;}
void vTaskDelay(unsigned ticks) {std::printf("delay %u\n",ticks);now+=ticks*1000;if(ticks==6)final_readback=true;}
namespace dg_board {
esp_err_t i2c::init() {std::puts("init_bus");return ESP_OK;}
i2c_master_bus_handle_t i2c::bus() {return reinterpret_cast<void*>(1);}
}
int gpio_set_level(gpio_num_t pin,unsigned level) {std::printf("gpio %d %u\n",pin,level);return ESP_OK;}
int gpio_set_direction(gpio_num_t pin,int dir) {std::printf("direction %d %d\n",pin,dir);return ESP_OK;}
int i2c_master_probe(i2c_master_bus_handle_t,unsigned addr,int timeout) {std::printf("probe %02x %d\n",addr,timeout);return ESP_OK;}
int i2c_master_bus_add_device(i2c_master_bus_handle_t,const i2c_device_config_t* cfg,i2c_master_dev_handle_t* out) {
    std::printf("add_device %02x %u\n",cfg->device_address,cfg->scl_speed_hz);*out=new TraceI2cDevice{cfg->device_address};return ESP_OK;
}
int i2c_master_transmit(i2c_master_dev_handle_t p,const uint8_t* data,size_t n,int timeout) {
    if(n!=2)throw std::runtime_error("expected register write");
    std::printf("write %02x %02x %02x timeout=%d\n",p->address,data[0],data[1],timeout);
    if(nack && p->address==0x18 && data[0]==0x44 && data[1]==0x08) {nack=false;return ESP_ERR_INVALID_RESPONSE;}
    registers[p->address][data[0]]=data[1];return ESP_OK;
}
int i2c_master_transmit_receive(i2c_master_dev_handle_t p,const uint8_t* reg,size_t n,uint8_t* data,size_t len,int timeout) {
    if(n!=1 || len!=1)throw std::runtime_error("expected register read");
    *data=registers[p->address][*reg];
    // Corrupt only the final assertion's readback, after mute/settling.
    if(final_readback && *reg==bad_register)*data^=1;
    std::printf("read %02x %02x %02x timeout=%d\n",p->address,*reg,*data,timeout);return ESP_OK;
}
namespace {
TraceI2cDevice adc{0x40};
audio_codec_ctrl_if_t adc_control{};
}
extern "C" const audio_codec_ctrl_if_t* audio_codec_new_i2c_ctrl(audio_codec_i2c_cfg_t* cfg) {
    std::printf("adc_control %02x %u %d\n",cfg->addr,cfg->port,cfg->clock_speed_hz);adc.address=cfg->addr>>1;
    adc_control.is_open=[](const audio_codec_ctrl_if_t*){return true;};
    adc_control.read_reg=[](const audio_codec_ctrl_if_t*,int reg,int,void* data,int len) {
        const uint8_t address=reg;return i2c_master_transmit_receive(&adc,&address,1,static_cast<uint8_t*>(data),len,20);
    };
    adc_control.write_reg=[](const audio_codec_ctrl_if_t*,int reg,int,void* data,int) {
        const uint8_t bytes[]={uint8_t(reg),*static_cast<uint8_t*>(data)};return i2c_master_transmit(&adc,bytes,2,20);
    };
    return &adc_control;
}
extern "C" const audio_codec_gpio_if_t* audio_codec_new_gpio() {static audio_codec_gpio_if_t gpio{};return &gpio;}
// A single ES8311 instance needs no reference-manager concurrency in this fake.
extern "C" int codec_ref_acquire(const audio_codec_ctrl_info_t*,codec_ref_stage_t) {return 1;}
extern "C" int codec_ref_release(const audio_codec_ctrl_info_t*,codec_ref_stage_t) {return 0;}
extern "C" void esp_codec_dev_sleep(int ms) {vTaskDelay(ms);}
int i2s_new_channel(const i2s_chan_config_t* cfg,i2s_chan_handle_t* out,i2s_chan_handle_t* in) {
    std::printf("new_channel %d role=%d dma=%d/%d clear=%d tx=%d rx=%d\n",cfg->id,cfg->role,cfg->dma_desc_num,cfg->dma_frame_num,cfg->auto_clear,bool(out),bool(in));
    input.port=output.port=cfg->id;input.paired=output.paired=out && in;
    if(out)*out=&output;if(in)*in=&input;return ESP_OK;
}
int i2s_channel_init_std_mode(i2s_chan_handle_t p,const i2s_std_config_t* cfg) {
    p->config=*cfg;if(p->tx && p->paired)p->role=I2S_ROLE_SLAVE;
    const auto& s=cfg->slot_cfg;const auto& g=cfg->gpio_cfg;
    std::printf("std %s rate=%u mclk=%d data=%d mode=%d slot=%d ws=%u pins=%d,%d,%d,%d,%d\n",label(p),cfg->clk_cfg.sample_rate_hz,cfg->clk_cfg.mclk_multiple,s.data_bit_width,s.slot_mode,s.slot_bit_width,s.ws_width,g.mclk,g.bclk,g.ws,g.dout,g.din);return ESP_OK;
}
int i2s_channel_get_info(i2s_chan_handle_t p,i2s_chan_info_t* info) {
    const auto& c=p->config;unsigned bits=c.slot_cfg.slot_bit_width?c.slot_cfg.slot_bit_width:c.slot_cfg.data_bit_width;
    *info={p->role,c.clk_cfg.sample_rate_hz*unsigned(c.clk_cfg.mclk_multiple),c.clk_cfg.sample_rate_hz*2*bits,p->tx?&input:&output};
    std::printf("info %s role=%d mclk=%u bclk=%u\n",label(p),info->role,info->mclk_hz,info->bclk_hz);return ESP_OK;
}
int i2s_channel_register_event_callback(i2s_chan_handle_t p,const i2s_event_callbacks_t* cb,void*) {
    p->callbacks=*cb;std::printf("callbacks %s sent=%d overflow=%d recv=%d overflow=%d\n",label(p),bool(cb->on_sent),bool(cb->on_send_q_ovf),bool(cb->on_recv),bool(cb->on_recv_q_ovf));return ESP_OK;
}
int i2s_channel_enable(i2s_chan_handle_t p) {std::printf("enable %s\n",label(p));return ESP_OK;}
int i2s_channel_disable(i2s_chan_handle_t p) {std::printf("disable %s\n",label(p));return ESP_OK;}
int i2s_del_channel(i2s_chan_handle_t p) {std::printf("delete %s\n",label(p));return ESP_OK;}
int i2s_channel_preload_data(i2s_chan_handle_t p,const void*,size_t n,size_t* bytes) {*bytes=n;std::printf("preload %s %zu\n",label(p),n);return ESP_OK;}
int i2s_channel_write(i2s_chan_handle_t p,const void* data,size_t n,size_t* bytes,unsigned timeout) {
    *bytes=n;std::printf("pcm %s %zu timeout=%u",label(p),n,timeout);const auto* pcm=static_cast<const int16_t*>(data);
    for(unsigned i=0;i<n/2;++i)std::printf(" %d",pcm[i]);std::puts("");return ESP_OK;
}
int i2s_channel_read(i2s_chan_handle_t p,void* data,size_t n,size_t* bytes,uint32_t timeout) {
    constexpr uint32_t words[]={0x0123fedc,0x45678000,0x80007fff,0xffff1357,0x7fff8000,0x00002468};
    *bytes=std::min(n,sizeof(words));std::memcpy(data,words,*bytes);std::printf("capture %s %zu timeout=%u\n",label(p),n,timeout);return ESP_OK;
}
namespace dg_audio {
EchoCanceller::EchoCanceller()=default;EchoCanceller::~EchoCanceller()=default;
void aec_reference_gain(unsigned db) {std::printf("aec_gain %u\n",db);}
bool EchoCanceller::process(const int16_t* mic,const int16_t* ref,size_t n,int16_t* out,const uint32_t*,AecReadTiming) {
    for(size_t i=0;i<n;++i) {std::printf("afe mic=%d ref=%d\n",mic[i],ref[i]);out[i]=mic[i];}return true;
}
}
int main(int argc,char** argv) {
    for(int i=1;i<argc;++i) {
        if(std::string(argv[i])=="--nack")nack=true;
        else bad_register=std::stoi(argv[i],nullptr,16);
    }
    try {
        dg_audio::Mic mic;dg_audio::Speaker speaker;
        int16_t pcm[]={123,-32768,32767},raw[3]{};
        mic.read(pcm,3,10,raw);
        for(unsigned i=0;i<3;++i)std::printf("sample %d raw=%d\n",pcm[i],raw[i]);
        std::printf("clips %llu limited=%llu adc=%llu\n",(unsigned long long)mic.clips(),(unsigned long long)mic.limited_samples(),(unsigned long long)mic.adc_clips());
        speaker.write(pcm,3,10);speaker.idle();
    } catch(const std::exception&) {std::puts("REJECTED");return 1;}
}
