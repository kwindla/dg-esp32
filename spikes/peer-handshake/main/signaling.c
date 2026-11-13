#include "spike.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include "sdkconfig.h"
#include "cJSON.h"
#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
static const char *TAG = "signal";
void *psalloc(size_t size) {
    void *p = heap_caps_calloc(1, size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    assert(p); return p;
}
/* Only signaling/control tasks use HTTP, never a peer callback or the owner loop. */
char *http_post(const char *path, const void *body, size_t size, const char *type) {
    char url[384];
    if (snprintf(url, sizeof(url), "%s%s", CONFIG_DG_SERVER_URL, path) >= sizeof(url)) return NULL;
    esp_http_client_config_t cfg = {.url=url, .timeout_ms=15000};
    esp_http_client_handle_t h = esp_http_client_init(&cfg);
    if (!h) return NULL;
    char *result = psalloc(32769);
    esp_http_client_set_method(h, HTTP_METHOD_POST);
    esp_http_client_set_header(h, "Content-Type", type);
    bool ok = esp_http_client_open(h, size) == ESP_OK;
    size_t n = 0;
    while (ok && n < size) {
        int w = esp_http_client_write(h, (const char *)body+n, size-n);
        if (w <= 0) ok = false; else n += w;
    }
    if (ok && esp_http_client_fetch_headers(h) < 0) ok = false;
    n = 0;
    while (ok && n < 32768) {
        int got = esp_http_client_read(h, result+n, 32768-n);
        if (got < 0) { ok = false; break; }
        if (!got) break;
        n += got;
    }
    int status = esp_http_client_get_status_code(h);
    ok = ok && status >= 200 && status < 300 && n < 32768 && esp_http_client_is_complete_data_received(h);
    ESP_LOGI(TAG, "HTTP status=%d bytes=%u stack=%u", status, (unsigned)n, (unsigned)uxTaskGetStackHighWaterMark(NULL));
    esp_http_client_cleanup(h);
    if (!ok) { free(result); return NULL; }
    return result;
}
bool start_session(spike *s) {
    char *r = http_post("/start", "{\"transport\":\"webrtc\"}", 22, "application/json");
    cJSON *j = r ? cJSON_Parse(r) : NULL;
    cJSON *id = cJSON_GetObjectItem(j, "sessionId");
    bool ok = cJSON_IsString(id) && strlen(id->valuestring)<sizeof(s->session);
    if (ok) strcpy(s->session, id->valuestring);
    cJSON_Delete(j); free(r); return ok;
}
static void evidence_text(spike *s, const char *kind, const char *text) {
    char path[192]; snprintf(path, sizeof(path), "/spike-evidence/%d/%s", s->attempt, kind);
    char *r = http_post(path, text, strlen(text), "text/plain"); free(r);
}
static void munge(char *sdp) {
#ifdef CONFIG_SPIKE_MUNGE
    char *read=sdp, *write=sdp;
    while (*read) {
        char *end = strstr(read, "\r\n");
        if (!end) break;
        size_t n = end-read+2;
        bool drop = !strncmp(read,"a=fingerprint:sha-384",21) || !strncmp(read,"a=fingerprint:sha-512",21);
        if (!strncmp(read,"a=candidate:",12)) {
            char *host = strstr(read," typ host");
            drop = !host || host >= end;
        }
        if (!drop) { memmove(write,read,n); write += n; }
        read = end+2;
    }
    *write = 0;
#else
    (void)sdp;
#endif
}
void signaling_task(void *arg) {
    spike *s = arg;
    text_event *ev=psalloc(sizeof(*ev));
    while (!atomic_load(&s->stop)) {
        if (!xQueueReceive(s->signal, ev, pdMS_TO_TICKS(100))) continue;
        if (ev->kind == 1) {
            ESP_LOGI(TAG, "OFFER attempt=%d\n%s", s->attempt, ev->text);
            evidence_text(s,"offer.sdp",ev->text);
#ifdef CONFIG_SPIKE_ACTIVE_OFFER
            // aiortc chooses SCTP initiator by ICE role; esp_peer follows DTLS role.
            // Offer active DTLS so our controlling ICE endpoint initiates both.
            char *setup;
            while ((setup=strstr(ev->text,"a=setup:actpass\r\n"))) {
                memcpy(setup,"a=setup:active",14);
                memmove(setup+14,setup+15,strlen(setup+15)+1);
            }
            evidence_text(s,"offer-active.sdp",ev->text);
#endif
            cJSON *j=cJSON_CreateObject();
            cJSON_AddStringToObject(j,"type","offer"); cJSON_AddStringToObject(j,"sdp",ev->text);
            char *body=cJSON_PrintUnformatted(j); cJSON_Delete(j);
            char path[192]; snprintf(path,sizeof(path),"/sessions/%s/api/offer",s->session);
            char *r=http_post(path,body,strlen(body),"application/json"); free(body);
            j=r?cJSON_Parse(r):NULL;
            cJSON *sdp=cJSON_GetObjectItem(j,"sdp"), *id=cJSON_GetObjectItem(j,"pc_id"), *type=cJSON_GetObjectItem(j,"type");
            if (cJSON_IsString(sdp) && cJSON_IsString(type) && !strcmp(type->valuestring,"answer") &&
                strlen(sdp->valuestring)<TEXT_CAP && strstr(sdp->valuestring,"a=candidate:")) {
                if (cJSON_IsString(id)) snprintf(s->pc_id,sizeof(s->pc_id),"%s",id->valuestring);
                evidence_text(s,"answer-original.sdp",sdp->valuestring);
                strcpy(ev->text,sdp->valuestring); munge(ev->text); ev->size=strlen(ev->text);
                evidence_text(s,"answer.sdp",ev->text);
                ESP_LOGI(TAG,"ANSWER attempt=%d pc_id=%s\n%s",s->attempt,s->pc_id,ev->text);
                if (!xQueueSend(s->answer,ev,0)) atomic_fetch_add(&s->drops,1);
            } else { ESP_LOGE(TAG,"Invalid answer or missing host candidate"); atomic_store(&s->stop,true); }
            cJSON_Delete(j); free(r); phase(s,"answer");
        } else {
            ESP_LOGI(TAG,"DATA %.*s",ev->size,ev->text);
            cJSON *j=cJSON_ParseWithLength(ev->text,ev->size);
            cJSON *type=cJSON_GetObjectItem(j,"type");
            if (cJSON_IsString(type) && !strcmp(type->valuestring,"bot-ready") && !atomic_load(&s->ready)) {
                s->ready_us=esp_timer_get_time(); atomic_store(&s->ready,true); phase(s,"bot-ready");
            }
            cJSON_Delete(j);
        }
    }
    phase(s,"signal-stop"); free(ev);
    xEventGroupSetBits(s->done,SIGNAL_STOP); vTaskDelete(NULL);
}
void save_evidence(spike *s) {
    char path[192]; snprintf(path,sizeof(path),"/spike-evidence/%d/rx.pcm",s->attempt);
    char *r=http_post(path,s->pcm,s->samples*2,"application/octet-stream"); free(r);
    char meta[512]; snprintf(meta,sizeof(meta),"{\"attempt\":%d,\"pc_id\":\"%s\",\"samples\":%u,\"decoded\":%u,\"tx\":%u,\"drops\":%u,\"errors\":%u,\"ready_us\":%lld,\"play_us\":%lld}",s->attempt,s->pc_id,s->samples,s->decoded,s->tx_count,atomic_load(&s->drops),s->errors,(long long)s->ready_us,(long long)s->play_us);
    evidence_text(s,"device.json",meta);
}
