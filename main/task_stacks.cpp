#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cstddef>
namespace {
struct Stack {TaskHandle_t handle;size_t bytes;};
Stack stacks[64]{};
portMUX_TYPE mux=portMUX_INITIALIZER_UNLOCKED;
void remember(TaskHandle_t handle,size_t bytes) {
    if(!handle)return;
    portENTER_CRITICAL(&mux);
    Stack* free=nullptr;
    for(auto& s:stacks) {
        if(s.handle==handle) {s.bytes=bytes;free=nullptr;break;}
        if(!s.handle && !free)free=&s;
    }
    if(free)*free={handle,bytes};
    portEXIT_CRITICAL(&mux);
}
}
size_t measured_task_stack(TaskHandle_t handle) {
    size_t bytes=0;portENTER_CRITICAL(&mux);
    for(const auto& s:stacks)if(s.handle==handle) {bytes=s.bytes;break;}
    portEXIT_CRITICAL(&mux);return bytes;
}
extern "C" {
BaseType_t __real_xTaskCreatePinnedToCore(TaskFunction_t,const char*,uint32_t,void*,UBaseType_t,TaskHandle_t*,BaseType_t);
BaseType_t __wrap_xTaskCreatePinnedToCore(TaskFunction_t fn,const char* name,uint32_t bytes,void* ctx,UBaseType_t prio,TaskHandle_t* out,BaseType_t core) {
    TaskHandle_t local=nullptr;auto result=__real_xTaskCreatePinnedToCore(fn,name,bytes,ctx,prio,out?out:&local,core);
    if(result==pdPASS)remember(out?*out:local,bytes);
    return result;
}
BaseType_t __real_xTaskCreatePinnedToCoreWithCaps(TaskFunction_t,const char*,uint32_t,void*,UBaseType_t,TaskHandle_t*,BaseType_t,UBaseType_t);
BaseType_t __wrap_xTaskCreatePinnedToCoreWithCaps(TaskFunction_t fn,const char* name,uint32_t bytes,void* ctx,UBaseType_t prio,TaskHandle_t* out,BaseType_t core,UBaseType_t caps) {
    TaskHandle_t local=nullptr;auto result=__real_xTaskCreatePinnedToCoreWithCaps(fn,name,bytes,ctx,prio,out?out:&local,core,caps);
    if(result==pdPASS)remember(out?*out:local,bytes);
    return result;
}
TaskHandle_t __real_xTaskCreateStaticPinnedToCore(TaskFunction_t,const char*,uint32_t,void*,UBaseType_t,StackType_t*,StaticTask_t*,BaseType_t);
TaskHandle_t __wrap_xTaskCreateStaticPinnedToCore(TaskFunction_t fn,const char* name,uint32_t bytes,void* ctx,UBaseType_t prio,StackType_t* stack,StaticTask_t* task,BaseType_t core) {
    auto handle=__real_xTaskCreateStaticPinnedToCore(fn,name,bytes,ctx,prio,stack,task,core);remember(handle,bytes);return handle;
}
void __real_vTaskDelete(TaskHandle_t);
void __wrap_vTaskDelete(TaskHandle_t task) {
    TaskHandle_t handle=task?task:xTaskGetCurrentTaskHandle();
    portENTER_CRITICAL(&mux);for(auto& s:stacks)if(s.handle==handle)s={};portEXIT_CRITICAL(&mux);
    __real_vTaskDelete(task);
}
}
