#include "pipecat/esp/platform.h"
#include "esp_pthread.h"
#include "pipecat/errors.h"
#include "esp_heap_caps.h"
namespace pipecat::esp {
void configure_threads(int core, int priority, size_t stack) {
    auto cfg = esp_pthread_get_default_config();
    cfg.pin_to_core = core;
    cfg.prio = priority;
    cfg.stack_size = stack;
    cfg.inherit_cfg = true;
    cfg.thread_name = "pipecat";
    // Core event/timer threads do not write flash; preserve internal RAM for
    // WiFi, DMA and the peer owner. Requires TASK_CREATE_ALLOW_EXT_MEM.
    cfg.stack_alloc_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
    if (esp_pthread_set_cfg(&cfg) != ESP_OK)
        throw PipecatError("Unable to configure Pipecat threads");
}
}
