#pragma once
#ifdef __cplusplus
extern "C" {
#endif
void audio_trace_log(const char*,const char*,...);
#ifdef __cplusplus
}
#endif
#define ESP_LOGI(...) audio_trace_log(__VA_ARGS__)
#define ESP_LOGW(...) audio_trace_log(__VA_ARGS__)
#define ESP_LOGE(...) audio_trace_log(__VA_ARGS__)
#define ESP_LOGD(...) ((void)0)
