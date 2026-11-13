#include "duo_settings.h"
#include "demo_settings.h"
#include "esp_heap_caps.h"
#include <cstdlib>
#include "nvs.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <future>
#include <thread>
#include <vector>
#include "check.h"
#include <cstring>
#include <cstdio>
namespace {
void(*worker)(void*)=nullptr;bool session=true,available=true;unsigned turns=0,writes=0,gates=0;
uint8_t persisted=1;unsigned tasks=0;size_t allocated=0;struct Stop{};
std::atomic<unsigned> notifications{0};std::vector<uint8_t> blobs[3];
void cycle(){turns=0;try{worker(nullptr);}catch(const Stop&){};}
}
unsigned ulTaskNotifyTake(int,TickType_t){if(turns++)throw Stop{};return notifications.exchange(0);}
void* heap_caps_malloc(size_t bytes,unsigned caps){CHECK(caps==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));allocated+=bytes;return std::malloc(bytes);}
int xTaskCreatePinnedToCore(void(*fn)(void*),const char* name,unsigned stack,void*,unsigned,TaskHandle_t* task,int){
    CHECK(!std::strcmp(name,"demo-config") && stack==4096);++tasks;worker=fn;*task=reinterpret_cast<void*>(1);return pdPASS;}
void xTaskNotifyGive(TaskHandle_t){++notifications;}
esp_err_t nvs_open(const char* space,int mode,nvs_handle_t* h){if(mode==NVS_READWRITE)CHECK(!session);*h=!std::strcmp(space,"dg-duo")?2:1;return ESP_OK;}
esp_err_t nvs_set_u8(nvs_handle_t,const char*,uint8_t value){CHECK(!session);persisted=value;++writes;return ESP_OK;}
esp_err_t nvs_get_u8(nvs_handle_t,const char*,uint8_t* value){*value=persisted;return ESP_OK;}
esp_err_t nvs_set_blob(nvs_handle_t h,const char*,const void* p,size_t size){CHECK(!session);
    auto* first=static_cast<const uint8_t*>(p);blobs[h].assign(first,first+size);return ESP_OK;}
esp_err_t nvs_get_blob(nvs_handle_t h,const char*,void* p,size_t* size){
    if(blobs[h].empty() || *size<blobs[h].size())return ESP_FAIL;
    *size=blobs[h].size();std::memcpy(p,blobs[h].data(),*size);return ESP_OK;}
esp_err_t nvs_commit(nvs_handle_t){CHECK(!session);return ESP_OK;}
void nvs_close(nvs_handle_t){}
namespace dg_display {
int idle_configuration_write(int(*write)(void*),void* context){++gates;
    if(session || !available)return ESP_ERR_INVALID_STATE;
    return write(context);
}
}
int main(){
    dg::demo_settings_init();cycle();CHECK(tasks==1 && gates==0);
    dg::duo_settings_init();CHECK(tasks==1 && allocated==2*sizeof(dg::DuoSettings));CHECK(dg::duo_last_character()==dg_duo::Persona::Devil);
    dg::duo_character_ack(dg_duo::Persona::Angel);
    for(unsigned n=0;n<100;++n)cycle();
    CHECK(writes==0 && gates==100);
    dg::duo_character_ack(dg_duo::Persona::Devil);cycle();CHECK(!writes);
    session=false;available=false;cycle();CHECK(!writes);
    available=true;cycle();CHECK(writes==1 && persisted==1);cycle();CHECK(writes==1);
    session=true;dg::duo_character_ack(dg_duo::Persona::Angel);cycle();CHECK(writes==1);
    session=false;cycle();CHECK(writes==2 && persisted==0);
    // Both namespaces share one real worker, with independent completion mailboxes.
    auto duo=dg::duo_settings();std::strcpy(duo.origin,"http://fixture:7874");
    auto demo=dg::demo_settings();demo.configured=true;std::strcpy(demo.url,"http://duo.invalid:7874");
    auto origin=std::async(std::launch::async,[&]{return dg::duo_settings_save(duo);});
    auto configuration=std::async(std::launch::async,[&]{return dg::demo_settings_save(demo);});
    auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
    while(settings_fake_waiters<2 && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
    CHECK(settings_fake_waiters==2);cycle();CHECK(origin.get()==ESP_OK && configuration.get()==ESP_OK);
    CHECK(!std::strcmp(dg::duo_settings().origin,duo.origin) && !std::strcmp(dg::demo_settings().url,demo.url));
    CHECK(tasks==1);
    session=true;std::strcpy(duo.origin,"http://another:7874");
    auto blocked=std::async(std::launch::async,[&]{return dg::duo_settings_save(duo);});
    deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
    while(!settings_fake_waiters && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
    CHECK(settings_fake_waiters==1);cycle();CHECK(blocked.get()==ESP_ERR_INVALID_STATE);
    CHECK(!std::strcmp(dg::duo_settings().origin,"http://fixture:7874"));
    std::puts("shared demo-config NVS worker: PSRAM, both namespaces, idle writes and busy retry passed");
}
