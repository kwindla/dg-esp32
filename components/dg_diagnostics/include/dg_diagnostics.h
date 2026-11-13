#pragma once
#include <stdint.h>
#include <stdbool.h>
#ifdef __cplusplus
extern "C" {
#endif
void dg_diagnostics_init(void);
void dg_deferred_log(const char *format, ...) __attribute__((format(printf,1,2)));
unsigned dg_deferred_log_drops(void);
void dg_heap_phase(const char *name, bool transport);
void dg_diagnostics_flush(void);
void dg_start_request(void);
void dg_bot_ready(void);
uint32_t dg_dtls_duration_us(void);
typedef struct {int64_t start_us,ready_us;} dg_connection_timings_t;
dg_connection_timings_t dg_connection_timings(void);
void dg_dtls_cancel(void);
#ifdef __cplusplus
}
#endif
