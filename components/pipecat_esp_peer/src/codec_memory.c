/* Native esp_audio_codec weak allocation hooks. The 2.6.2 archive defines these
 * weakly in audio_codec_mem.c.obj; signatures are from Espressif's media_lib_os.h:
 * https://github.com/espressif/esp-adf-libs/blob/master/media_lib_sal/include/media_lib_os.h
 * Force Opus state into PSRAM, independently of the global malloc threshold.
 * No process-wide malloc wrapper and no changes to peer or DMA allocations. */
#include "esp_heap_caps.h"
#include "esp_memory_utils.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdatomic.h>
#include <stddef.h>
#include "sdkconfig.h"
#if CONFIG_DG_CODEC_LAYOUT_TRACE
#include "dg_diagnostics.h"
#include "esp_timer.h"
#include "codec_memory_layout.h"
#endif
static _Atomic(TaskHandle_t) benchmark_owner;
static unsigned benchmark_caps;
static unsigned allocation_count, internal_count, external_count;
static size_t requested_bytes;
/* Only the disconnected benchmark task changes placement. Peer and normal
 * codec allocations retain their original explicit-PSRAM policy. */
void pipecat_codec_benchmark_begin(int internal) {
    benchmark_caps = (internal ? MALLOC_CAP_INTERNAL : MALLOC_CAP_SPIRAM) | MALLOC_CAP_8BIT;
    allocation_count = internal_count = external_count = 0; requested_bytes = 0;
    atomic_store(&benchmark_owner, xTaskGetCurrentTaskHandle());
}
void pipecat_codec_benchmark_end(void) { atomic_store(&benchmark_owner, NULL); }
void pipecat_codec_benchmark_allocations(unsigned *count, size_t *bytes, unsigned *internal, unsigned *external) {
    *count=allocation_count; *bytes=requested_bytes; *internal=internal_count; *external=external_count;
}
#if CONFIG_DG_CODEC_LAYOUT_TRACE
/* Each constructor records into its own stack context. A TLS pointer keeps
 * concurrent encoder/decoder allocation order visible without serializing it.
 * Only addresses/sizes are retained; allocation policy remains unchanged. */
static _Thread_local CodecLayoutContext *layout_context;
void pipecat_codec_layout_begin(CodecLayoutContext *context,unsigned role) {
    context->role=role;context->count=0;context->begin_us=esp_timer_get_time();
    layout_context=context;
}
void pipecat_codec_layout_end(CodecLayoutContext *context) {
    int64_t end_us=esp_timer_get_time();layout_context=NULL;
    for(unsigned i=0;i<context->count && i<16;++i)
        dg_deferred_log("CODEC_LAYOUT role=%u index=%u ptr=%p bytes=%u",context->role,i,context->rows[i].pointer,(unsigned)context->rows[i].bytes);
    dg_deferred_log("CODEC_LAYOUT_END role=%u count=%u truncated=%u begin_us=%lld end_us=%lld",context->role,context->count,context->count>16,(long long)context->begin_us,(long long)end_us);
}
#endif
static int benchmarking(void) {
    TaskHandle_t owner=atomic_load(&benchmark_owner);
    return owner && owner == xTaskGetCurrentTaskHandle();
}
static unsigned caps(void) { return benchmarking() ? benchmark_caps : MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT; }
static void *record(void *ptr, size_t size) {
#if CONFIG_DG_CODEC_LAYOUT_TRACE
    if(ptr && layout_context) {
        unsigned index=layout_context->count++;
        if(index<16)layout_context->rows[index]=(CodecLayoutRow){ptr,size};
    }
#endif
    if (ptr && benchmarking()) {
        ++allocation_count; requested_bytes += size;
        if (esp_ptr_internal(ptr)) ++internal_count;
        else if (esp_ptr_external_ram(ptr)) ++external_count;
    }
    return ptr;
}
void pipecat_codec_memory_init(void) {} /* forces this object into the link */
void *media_lib_module_malloc(const char *module, size_t size) {
    (void)module;
    return record(heap_caps_malloc(size, caps()), size);
}
void *media_lib_module_calloc(const char *module, size_t count, size_t size) {
    (void)module;
    return record(heap_caps_calloc(count, size, caps()), count * size);
}
void *media_lib_module_realloc(const char *module, void *ptr, size_t size) {
    (void)module;
    return record(heap_caps_realloc(ptr, size, caps()), size);
}
/* esp_peer uses the same native media-lib hooks for its caches and packet
 * storage. Synchronization objects still use IDF's internal heap directly. */
void *media_lib_malloc(size_t size) {
    return record(heap_caps_malloc(size, caps()), size);
}
void *media_lib_calloc(size_t count, size_t size) {
    return record(heap_caps_calloc(count, size, caps()), count * size);
}
void *media_lib_realloc(void *ptr, size_t size) {
    return record(heap_caps_realloc(ptr, size, caps()), size);
}
