#pragma once
#include <stdbool.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
enum { DG_WIFI_OBSERVE_CAPACITY=256, DG_WIFI_OBSERVE_STACK_BYTES=4096 };
typedef struct {
    uint64_t sequence;
    int64_t counter_begin_us,begin_us,end_us;
    int ps_error,ap_error,ps_mode;
    int rssi_dbm,primary_channel,secondary_channel;
    uint8_t bssid[6];
    uint32_t tx_done_success,tx_done_failed;
} dg_wifi_observation_t;
typedef struct {
    bool enabled,tx_done_registered;
    int startup_error;
    uint64_t samples,overwritten,first_sequence,last_sequence;
    unsigned retained,capacity,record_bytes;
    int static_tx_buffers,cache_tx_buffers,static_rx_buffers,dynamic_rx_buffers;
    int tx_buffer_type,ampdu_tx,ampdu_rx,rx_ba_window,tx_ba_window,wifi_core;
} dg_wifi_observation_status_t;
// Copy-only reads: no WiFi API, driver dump, allocation or log output.
dg_wifi_observation_status_t dg_wifi_observe_status(void);
// Explicit sequence detects overwrite during an export; never fabricate gaps.
bool dg_wifi_observe_read(uint64_t sequence,dg_wifi_observation_t* output);
#ifdef __cplusplus
}
#endif
