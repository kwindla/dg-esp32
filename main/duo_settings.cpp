#include "duo_settings.h"
#include "dg_display/runtime.h"
#include "nvs.h"
#include "esp_heap_caps.h"
#include <new>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include <atomic>
#include <cstring>
#include <cstdlib>
#include <mutex>
namespace dg {
namespace {
struct Values {DuoSettings current{},requested{};};
std::atomic<Values*> values{nullptr};std::mutex guard;
std::atomic<bool> request_pending{false};
StaticSemaphore_t completed_storage;SemaphoreHandle_t completed;
dg_duo::DeferredCharacter character_write;
dg_duo::Persona last_character=dg_duo::Persona::Angel;
int store_character(void* value){
    nvs_handle_t h=0;auto e=nvs_open("dg-duo",NVS_READWRITE,&h);
    auto c=*static_cast<uint8_t*>(value);
    if(e==ESP_OK)e=nvs_set_u8(h,"character",c);
    if(e==ESP_OK)e=nvs_commit(h);
    uint8_t verify=255;if(e==ESP_OK)e=nvs_get_u8(h,"character",&verify);
    if(e==ESP_OK && verify!=c)e=ESP_FAIL;
    if(h)nvs_close(h);
    return e;
}
std::atomic<bool> busy{false};std::atomic<esp_err_t> saved{ESP_FAIL};
int store(void*){
    auto& requested=values.load()->requested;
    nvs_handle_t h=0;auto e=nvs_open("dg-duo",NVS_READWRITE,&h);
    if(e==ESP_OK)e=nvs_set_blob(h,"settings",&requested,sizeof(requested));
    if(e==ESP_OK)e=nvs_commit(h);
    DuoSettings verify{};size_t size=sizeof(verify);
    if(e==ESP_OK)e=nvs_get_blob(h,"settings",&verify,&size);
    if(e==ESP_OK && (size!=sizeof(verify) || std::memcmp(&requested,&verify,size)))e=ESP_FAIL;
    if(h)nvs_close(h);
    return e;
}
}
// Called by the existing internal-stack demo-config owner, only in duo images.
void duo_settings_poll(){
    auto* state=values.load(std::memory_order_acquire);if(!state)return;
    if(request_pending.exchange(false)){
        auto e=dg_display::idle_configuration_write(store,nullptr);
        {std::lock_guard<std::mutex> lock(guard);if(e==ESP_OK)state->current=state->requested;}
        saved=e;xSemaphoreGive(completed);busy=false;
    }
    // The duo session-start path takes the same gate before setting active.
    character_write.flush(false,[](dg_duo::Persona c){
        auto value=uint8_t(c);return dg_display::idle_configuration_write(store_character,&value)==ESP_OK;
    });
}
bool duo_valid_origin(const char* s)noexcept{
    if(!s || !*s)return s!=nullptr;
    if(std::strncmp(s,"http://",7) || std::strlen(s)>=192)return false;
    // Same HTTP authority policy as the display origin; no paths/credentials.
    const char* host=s+7;const char* p=host;
    while((*p>='a' && *p<='z') || (*p>='A' && *p<='Z') || (*p>='0' && *p<='9') || *p=='.' || *p=='-')++p;
    if(p==host)return false;
    if(*p==':'){unsigned port=0;++p;const char* start=p;
        while(*p>='0' && *p<='9'){port=port*10+unsigned(*p++-'0');if(port>65535)return false;}
        if(p==start || !port)return false;
    }return !*p;
}
dg_duo::Persona duo_last_character(){return last_character;}
void duo_character_ack(dg_duo::Persona c){character_write.acknowledge(c);}
void duo_settings_init(){
    void* memory=heap_caps_malloc(sizeof(Values),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if(!memory)throw std::bad_alloc();
    auto* state=new(memory) Values;
    nvs_handle_t h=0;DuoSettings candidate{};size_t size=sizeof(candidate);
    if(nvs_open("dg-duo",NVS_READONLY,&h)==ESP_OK){auto e=nvs_get_blob(h,"settings",&candidate,&size);nvs_close(h);
        if(e==ESP_OK && size==sizeof(candidate) && candidate.version==1 && candidate.origin[191]==0 && duo_valid_origin(candidate.origin))state->current=candidate;
    }
    uint8_t persona=0;
    if(nvs_open("dg-duo",NVS_READONLY,&h)==ESP_OK){
        auto e=nvs_get_u8(h,"character",&persona);nvs_close(h);
        if(e==ESP_OK && persona<=1)last_character=dg_duo::Persona(persona);
    }
    completed=xSemaphoreCreateBinaryStatic(&completed_storage);
    values.store(state,std::memory_order_release);
}
DuoSettings duo_settings(){std::lock_guard<std::mutex> lock(guard);return values.load()->current;}
esp_err_t duo_settings_save(const DuoSettings& s){
    if(!values.load() || s.version!=1 || !std::memchr(s.origin,0,sizeof(s.origin)) || !duo_valid_origin(s.origin))return ESP_ERR_INVALID_ARG;
    bool expected=false;if(!busy.compare_exchange_strong(expected,true))return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(completed,0);values.load()->requested=s;request_pending.store(true,std::memory_order_release);
    if(xSemaphoreTake(completed,pdMS_TO_TICKS(5000))!=pdTRUE)return ESP_ERR_TIMEOUT;
    return saved.load();
}
}
