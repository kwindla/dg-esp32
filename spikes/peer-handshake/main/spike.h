#pragma once
#include <stdatomic.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_peer.h"
#include "esp_timer.h"
#define PCM_CAP (16000 * 24)
#define SIGNAL_STOP 1
#define TX_STOP 2
#define RX_STOP 4
#define LOOP_STOP 8
#define ALL_STOP 15
#define TEXT_CAP 16384
#define PACKET_CAP 1500
typedef struct { int kind, size; char text[TEXT_CAP]; } text_event;
typedef struct { int kind, value; int64_t time; } control_event;
typedef struct { unsigned pts; int size; uint8_t data[PACKET_CAP]; } packet;
typedef struct {
    esp_peer_handle_t peer;
    QueueHandle_t signal, answer, control, tx, rx;
    EventGroupHandle_t done;
    TaskHandle_t signal_task, tx_task, rx_task, loop_task;
    atomic_bool stop, ready;
    atomic_uint drops;
    char session[96], pc_id[128];
    int attempt;
    int16_t *pcm;
    unsigned samples, decoded, tx_count, errors;
    int64_t ready_us, play_us;
    unsigned loop_hist[1001], loop_max;
    uint16_t stream;
} spike;
void phase(spike *s, const char *name);
void signaling_task(void *arg);
void audio_tx_task(void *arg);
void audio_rx_task(void *arg);
char *http_post(const char *path, const void *body, size_t size, const char *type);
bool start_session(spike *s);
void save_evidence(spike *s);
void *psalloc(size_t size);
