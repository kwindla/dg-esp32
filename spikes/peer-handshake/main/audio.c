#include "spike.h"
#include <math.h>
#include <string.h>
#include "sdkconfig.h"
#include "esp_opus_enc.h"
#include "esp_opus_dec.h"
#include "esp_log.h"
static const char *TAG="audio";
static void tick(void *arg) { xTaskNotifyGive((TaskHandle_t)arg); }
void audio_tx_task(void *arg) {
    spike *s=arg;
    void *enc=NULL;
    esp_opus_enc_config_t cfg=ESP_OPUS_ENC_CONFIG_DEFAULT();
    cfg.sample_rate=16000; cfg.channel=1; cfg.bitrate=24000; cfg.complexity=0;
    ESP_ERROR_CHECK(esp_opus_enc_open(&cfg,sizeof(cfg),&enc));
    int16_t *pcm=psalloc(640);
    packet *p=psalloc(sizeof(*p));
    for (int i=0;i<320;i++) pcm[i]=(int16_t)(8000*sin(2*M_PI*1000*i/16000));
    esp_timer_handle_t timer;
    esp_timer_create_args_t tc={.callback=tick,.arg=xTaskGetCurrentTaskHandle(),.name="tone-clock"};
    ESP_ERROR_CHECK(esp_timer_create(&tc,&timer));
    while (!atomic_load(&s->ready) && !atomic_load(&s->stop)) vTaskDelay(pdMS_TO_TICKS(20));
    ESP_ERROR_CHECK(esp_timer_start_periodic(timer,20000));
    unsigned count=0; phase(s,"encode-ready");
    while (!atomic_load(&s->stop) && count<500) {
        uint32_t ticks=ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(100));
        if (!ticks) continue;
        if(ticks!=1) atomic_fetch_add(&s->drops,ticks-1);
        // Exactly 10 seconds, including 1 second of silence at either boundary.
        int16_t silence[320]={0};
        esp_audio_enc_in_frame_t in={.buffer=(uint8_t*)((count>=50&&count<450)?pcm:silence),.len=640};
        esp_audio_enc_out_frame_t out={.buffer=p->data,.len=PACKET_CAP};
        ESP_ERROR_CHECK(esp_opus_enc_process(enc,&in,&out));
#ifdef CONFIG_SPIKE_PTS_MS
        p->pts=count*20;
#else
        p->pts=count*960;
#endif
        p->size=out.encoded_bytes;
        if (!xQueueSend(s->tx,p,0)) atomic_fetch_add(&s->drops,1);
        if(count<2) phase(s,"encoded-frame");
        count++;
    }
    ESP_ERROR_CHECK(esp_timer_stop(timer)); ESP_ERROR_CHECK(esp_timer_delete(timer));
    phase(s,"tx-stop"); esp_opus_enc_close(enc); free(pcm); free(p);
    xEventGroupSetBits(s->done,TX_STOP); vTaskDelete(NULL);
}
void audio_rx_task(void *arg) {
    spike *s=arg; void *dec=NULL;
    esp_opus_dec_cfg_t cfg={.sample_rate=16000,.channel=1,.frame_duration=ESP_OPUS_DEC_FRAME_DURATION_20_MS};
    ESP_ERROR_CHECK(esp_opus_dec_open(&cfg,sizeof(cfg),&dec));
    packet *p=psalloc(sizeof(*p)); uint8_t *pcm=psalloc(4096);
    while (!atomic_load(&s->stop) || uxQueueMessagesWaiting(s->rx)) {
        if (!xQueueReceive(s->rx,p,pdMS_TO_TICKS(100))) continue;
        esp_audio_dec_in_raw_t in={.buffer=p->data,.len=p->size};
        esp_audio_dec_out_frame_t out={.buffer=pcm,.len=4096}; esp_audio_dec_info_t info={0};
        int err=esp_opus_dec_decode(dec,&in,&out,&info);
        if(err || in.consumed!=p->size || info.sample_rate!=16000 || info.channel!=1) {
            ESP_LOGE(TAG,"decode err=%d consumed=%lu rate=%lu channel=%u",err,(unsigned long)in.consumed,(unsigned long)info.sample_rate,info.channel); s->errors++; continue;
        }
        unsigned n=out.decoded_size/2;
        if(s->samples+n<=PCM_CAP) { memcpy(s->pcm+s->samples,pcm,n*2); s->samples+=n; }
        else s->errors++;
        s->decoded++;
    }
    ESP_LOGI(TAG,"DECODE samples=%u packets=%u errors=%u",s->samples,s->decoded,s->errors);
    phase(s,"rx-stop"); esp_opus_dec_close(dec); free(p); free(pcm);
    xEventGroupSetBits(s->done,RX_STOP); vTaskDelete(NULL);
}
