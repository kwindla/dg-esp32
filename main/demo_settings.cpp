#include "demo_settings.h"
#include "sdkconfig.h"
#if CONFIG_DG_DUO
#include "duo_settings.h"
#include <cstdlib>
#endif
#include "dg_display/runtime.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include <atomic>
#include <cstring>
#include <mutex>
namespace dg {
namespace {
DemoSettings current{},requested{};
std::mutex guard;
TaskHandle_t writer;
StaticSemaphore_t completed_storage;
SemaphoreHandle_t completed;
std::atomic<bool> busy{false};
std::atomic<esp_err_t> saved{ESP_FAIL};
int store(void*) {
    nvs_handle_t handle=0;auto error=nvs_open("dg-demo",NVS_READWRITE,&handle);
    if(error==ESP_OK)error=nvs_set_blob(handle,"settings",&requested,sizeof(requested));
    if(error==ESP_OK)error=nvs_commit(handle);
    DemoSettings verify{};size_t size=sizeof(verify);
    if(error==ESP_OK)error=nvs_get_blob(handle,"settings",&verify,&size);
    if(error==ESP_OK && (size!=sizeof(verify) || std::memcmp(&verify,&requested,size)))error=ESP_FAIL;
    if(handle)nvs_close(handle);
    return error;
}
void run(void*) {
    for(;;) {
#if CONFIG_DG_DUO
        if(ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(1000))){
#else
        ulTaskNotifyTake(pdTRUE,portMAX_DELAY);
#endif
        const auto error=dg_display::idle_configuration_write(store,nullptr);
        {std::lock_guard<std::mutex> lock(guard);if(error==ESP_OK)current=requested;}
        saved=error;xSemaphoreGive(completed);busy=false;
#if CONFIG_DG_DUO
        }
        duo_settings_poll();
#endif
    }
}
}
void demo_settings_init() {
    current.enabled=
#if CONFIG_DG_DEMO_AUTOSTART
        true;
#else
        false;
#endif

    std::strncpy(current.url,CONFIG_DG_SERVER_URL,sizeof(current.url)-1);
#if CONFIG_DG_DEMO_COMPILED_URL
    // Public demo images: the compiled server URL counts as configured, so a fresh device connects at power-on.
    current.configured=current.url[0]!=0;
#endif
    nvs_handle_t handle=0;DemoSettings candidate{};size_t size=sizeof(candidate);
    if(nvs_open("dg-demo",NVS_READONLY,&handle)==ESP_OK) {
        auto error=nvs_get_blob(handle,"settings",&candidate,&size);nvs_close(handle);
        if(error==ESP_OK && size==sizeof(candidate) && candidate.version==1 &&
           candidate.url[sizeof(candidate.url)-1]==0 && candidate.volume_db>=-36 && candidate.volume_db<=0)
            current=candidate;
    }
    completed=xSemaphoreCreateBinaryStatic(&completed_storage);
    if(xTaskCreatePinnedToCore(run,"demo-config",4096,nullptr,2,&writer,0)!=pdPASS)abort();
}
DemoSettings demo_settings() {std::lock_guard<std::mutex> lock(guard);return current;}
esp_err_t demo_settings_save(const DemoSettings& settings) {
    if(!writer || settings.version!=1 || !settings.configured || settings.volume_db< -36 || settings.volume_db>0 ||
       !std::memchr(settings.url,0,sizeof(settings.url)) ||
       (std::strncmp(settings.url,"http://",7) && std::strncmp(settings.url,"https://",8)))return ESP_ERR_INVALID_ARG;
    bool expected=false;if(!busy.compare_exchange_strong(expected,true))return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(completed,0);requested=settings;xTaskNotifyGive(writer);
    if(xSemaphoreTake(completed,pdMS_TO_TICKS(5000))!=pdTRUE)return ESP_ERR_TIMEOUT;
    return saved.load();
}
}
