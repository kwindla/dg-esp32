#include "spike.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sdkconfig.h"
#include "dg_net.h"
#include "esp_peer_default.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "cJSON.h"
static const char *TAG="spike";
static text_event *scratch;
static packet *audio_scratch;
void phase(spike *s,const char *name) {
    ESP_LOGI(TAG,"PHASE %s attempt=%d t=%lld int=%u largest=%u min=%u dma=%u psram=%u psram_largest=%u task=%s stack=%u",
        name,s?s->attempt:0,(long long)esp_timer_get_time(),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
        (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_DMA),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM),pcTaskGetName(NULL),
        (unsigned)uxTaskGetStackHighWaterMark(NULL));
}
static int control(spike *s,int kind,int value) {
    control_event e={kind,value,esp_timer_get_time()};
    if(!xQueueSend(s->control,&e,0)) atomic_fetch_add(&s->drops,1);
    return 0;
}
static int state(esp_peer_state_t value,void *ctx) { return control(ctx,1,value); }
static int opened(esp_peer_data_channel_info_t *ch,void *ctx) { return control(ctx,2,ch->stream_id); }
static int closed(esp_peer_data_channel_info_t *ch,void *ctx) { return control(ctx,3,ch->stream_id); }
static int info(esp_peer_audio_stream_info_t *in,void *ctx) {
    return control(ctx,4,(in->codec<<24)|(in->channel<<20)|in->sample_rate);
}
static int queue_text(spike *s,int kind,const void *data,int size) {
    if(atomic_load(&s->stop)) return 0;
    if(size<0 || size>=TEXT_CAP) { atomic_fetch_add(&s->drops,1); return -1; }
    scratch->kind=kind; scratch->size=size; memcpy(scratch->text,data,size); scratch->text[size]=0;
    if(!xQueueSend(s->signal,scratch,0)) atomic_fetch_add(&s->drops,1);
    return 0;
}
static int message(esp_peer_msg_t *m,void *ctx) {
    return m->type==ESP_PEER_MSG_TYPE_SDP?queue_text(ctx,1,m->data,m->size):0;
}
static int data(esp_peer_data_frame_t *f,void *ctx) { return queue_text(ctx,2,f->data,f->size); }
static int audio(esp_peer_audio_frame_t *f,void *ctx) {
    spike *s=ctx;
    if(atomic_load(&s->stop)) return 0;
    if(f->size<0 || f->size>PACKET_CAP) { atomic_fetch_add(&s->drops,1); return -1; }
    audio_scratch->pts=f->pts; audio_scratch->size=f->size; memcpy(audio_scratch->data,f->data,f->size);
    if(!xQueueSend(s->rx,audio_scratch,0)) atomic_fetch_add(&s->drops,1);
    return 0;
}
static void send_data(spike *s,const char *text) {
    esp_peer_data_frame_t f={.type=ESP_PEER_DATA_CHANNEL_STRING,.stream_id=s->stream,.data=(uint8_t*)text,.size=strlen(text)};
    int err=esp_peer_send_data(s->peer,&f);
    ESP_LOGI(TAG,"SEND stream=%u err=%d %s",s->stream,err,text);
    if(err) atomic_fetch_add(&s->drops,1);
}
static void owner(void *arg) {
    spike *s=arg; text_event *answer=psalloc(sizeof(*answer)); packet *p=psalloc(sizeof(*p));
    ESP_ERROR_CHECK(esp_peer_new_connection(s->peer)); phase(s,"new-connection");
    int64_t start=esp_timer_get_time(); bool played=false, leaving=false;
    while(!atomic_load(&s->stop)) {
        int64_t t=esp_timer_get_time(); int err=esp_peer_main_loop(s->peer);
        unsigned us=esp_timer_get_time()-t;
        s->loop_hist[us/1000<1000?us/1000:1000]++; if(us>s->loop_max)s->loop_max=us;
        if(err) { ESP_LOGE(TAG,"loop=%d",err); atomic_store(&s->stop,true); }
        control_event e;
        while(xQueueReceive(s->control,&e,0)) {
            ESP_LOGI(TAG,"EVENT kind=%d value=%d t=%lld",e.kind,e.value,(long long)e.time);
            if(e.kind==4) ESP_LOGI(TAG,"AUDIO_INFO codec=%d rate=%d channels=%d",e.value>>24,e.value&0xfffff,(e.value>>20)&15);
            if(e.kind==1 && e.value==ESP_PEER_STATE_CONNECTED) phase(s,"connected");
            if(e.kind==1 && e.value==ESP_PEER_STATE_DATA_CHANNEL_CONNECTED) {
                esp_peer_data_channel_cfg_t ch={.type=ESP_PEER_DATA_CHANNEL_RELIABLE,.ordered=true,.label="chat"};
                ESP_ERROR_CHECK(esp_peer_create_data_channel(s->peer,&ch));
            }
            if(e.kind==2) {
                s->stream=e.value;
                // Core v1.1.0 Message::client_ready serialization: RTVI 2.1.0.
                send_data(s,"{\"id\":\"spike-ready\",\"label\":\"rtvi-ai\",\"type\":\"client-ready\",\"data\":{\"version\":\"2.1.0\",\"about\":{\"library\":\"pipecat-client-cxx\",\"library_version\":\"1.1.0\"}}}");
                phase(s,"channel-open");
            }
            if(e.kind==3 || (e.kind==1 && (e.value==ESP_PEER_STATE_CONNECT_FAILED || e.value==ESP_PEER_STATE_DISCONNECTED || e.value==ESP_PEER_STATE_DATA_CHANNEL_DISCONNECTED))) atomic_store(&s->stop,true);
        }
        if(xQueueReceive(s->answer,answer,0)) {
            esp_peer_msg_t m={.type=ESP_PEER_MSG_TYPE_SDP,.data=(uint8_t*)answer->text,.size=answer->size};
            ESP_ERROR_CHECK(esp_peer_send_msg(s->peer,&m)); phase(s,"answer-applied");
        }
        while(xQueueReceive(s->tx,p,0)) {
            esp_peer_audio_frame_t f={.pts=p->pts,.data=p->data,.size=p->size};
            if(esp_peer_send_audio(s->peer,&f)) atomic_fetch_add(&s->drops,1); else s->tx_count++;
        }
        if(atomic_load(&s->ready)) {
            int64_t elapsed=esp_timer_get_time()-s->ready_us;
            if(elapsed>5000000 && !played) {
                s->play_us=esp_timer_get_time(); played=true;
                send_data(s,"{\"id\":\"spike-play\",\"label\":\"rtvi-ai\",\"type\":\"client-message\",\"data\":{\"t\":\"play\",\"d\":{\"fixture\":\"reply_pineapple.wav\"}}}");
                phase(s,"play");
            }
            if(elapsed>11700000 && !leaving) {
                leaving=true;
                send_data(s,"{\"id\":\"spike-disconnect\",\"label\":\"rtvi-ai\",\"type\":\"disconnect-bot\",\"data\":{}}");
            }
            if(elapsed>12000000) atomic_store(&s->stop,true);
        } else if(esp_timer_get_time()-start>25000000) { ESP_LOGE(TAG,"ready timeout"); atomic_store(&s->stop,true); }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    // Reject new work, then join the workers before disconnecting on the owner.
    xEventGroupWaitBits(s->done,SIGNAL_STOP|TX_STOP|RX_STOP,pdFALSE,pdTRUE,portMAX_DELAY);
    ESP_LOGI(TAG,"disconnect=%d",esp_peer_disconnect(s->peer)); phase(s,"disconnected");
    // The loop has stopped; close stays on this owner to avoid concurrent access.
    ESP_LOGI(TAG,"close=%d",esp_peer_close(s->peer)); s->peer=NULL;
    unsigned total=0; for(int i=0;i<1001;i++)total+=s->loop_hist[i];
    unsigned cumulative=0,p50=0,p95=0;
    for(int i=0;i<1001;i++) { cumulative+=s->loop_hist[i]; if(!p50&&cumulative>=total*.5)p50=i+1; if(!p95&&cumulative>=total*.95)p95=i+1; }
    ESP_LOGI(TAG,"LOOP count=%u p50_lt_ms=%u p95_lt_ms=%u max_us=%u",total,p50,p95,s->loop_max);
    phase(s,"closed"); free(answer); free(p);
    xEventGroupSetBits(s->done,LOOP_STOP); vTaskDelete(NULL);
}
static QueueHandle_t queue(unsigned count,unsigned size,void **storage,StaticQueue_t *ctl) {
    *storage=psalloc(count*size); return xQueueCreateStatic(count,size,*storage,ctl);
}
void app_main(void) {
    ESP_LOGI(TAG,"READY peer-handshake"); phase(NULL,"boot");
    cJSON_Hooks hooks={.malloc_fn=psalloc,.free_fn=free}; cJSON_InitHooks(&hooks);
    if(!strlen(CONFIG_DG_WIFI_SSID)||!strlen(CONFIG_DG_SERVER_URL)) { ESP_LOGW(TAG,"No local configuration; idle"); return; }
    esp_log_level_set("wifi",ESP_LOG_ERROR);
    ESP_ERROR_CHECK(dg_net_init(CONFIG_DG_WIFI_SSID,CONFIG_DG_WIFI_PASSWORD)); phase(NULL,"wifi-init");
    int64_t t=esp_timer_get_time(); ESP_ERROR_CHECK(esp_peer_pre_generate_cert());
    ESP_LOGI(TAG,"CERT us=%lld",(long long)(esp_timer_get_time()-t)); phase(NULL,"certificate");
    ESP_ERROR_CHECK(dg_net_wifi_connect(15000)); phase(NULL,"wifi-connected");
    scratch=psalloc(sizeof(*scratch)); audio_scratch=psalloc(sizeof(*audio_scratch));
    for(int attempt=1;attempt<=10;attempt++) {
        spike *s=psalloc(sizeof(*s)); s->attempt=attempt; s->pcm=psalloc(PCM_CAP*2);
        s->done=xEventGroupCreate();
        void *storage[4]; StaticQueue_t q[4];
        s->signal=queue(8,sizeof(text_event),&storage[0],&q[0]);
        s->answer=queue(1,sizeof(text_event),&storage[1],&q[1]);
        s->control=xQueueCreate(32,sizeof(control_event));
        s->tx=queue(16,sizeof(packet),&storage[2],&q[2]); s->rx=queue(32,sizeof(packet),&storage[3],&q[3]);
        phase(s,"start");
        if(!start_session(s)) { ESP_LOGE(TAG,"start failed"); abort(); }
        esp_peer_default_cfg_t extra={.agent_recv_timeout=100,
            .data_ch_cfg={.send_cache_size=32768,.recv_cache_size=32768,.cache_timeout=5000},
            .rtp_cfg={.audio_recv_jitter={.cache_size=16384,.cache_timeout=100},.send_pool_size=32768,.send_queue_num=64,.max_resend_count=3},.alive_binding_retries=5};
        esp_peer_cfg_t cfg={.role=ESP_PEER_ROLE_CONTROLLING,.audio_info={.codec=ESP_PEER_AUDIO_CODEC_OPUS,.sample_rate=16000,.channel=1},
            .audio_dir=ESP_PEER_MEDIA_DIR_SEND_RECV,.enable_data_channel=true,.manual_ch_create=true,.no_auto_reconnect=true,
            .extra_cfg=&extra,.extra_size=sizeof(extra),.ctx=s,.on_state=state,.on_msg=message,.on_audio_info=info,
            .on_audio_data=audio,.on_channel_open=opened,.on_channel_close=closed,.on_data=data};
        ESP_ERROR_CHECK(esp_peer_open(&cfg,esp_peer_get_default_impl(),&s->peer)); phase(s,"open");
        assert(xTaskCreatePinnedToCore(signaling_task,"signal",8192,s,5,&s->signal_task,0)==pdPASS);
        assert(xTaskCreatePinnedToCore(audio_tx_task,"encode",32768,s,11,&s->tx_task,1)==pdPASS);
        assert(xTaskCreatePinnedToCore(audio_rx_task,"decode",16384,s,10,&s->rx_task,1)==pdPASS);
        assert(xTaskCreatePinnedToCore(owner,"peer-loop",10240,s,6,&s->loop_task,0)==pdPASS);
        xEventGroupWaitBits(s->done,ALL_STOP,pdFALSE,pdTRUE,portMAX_DELAY);
        save_evidence(s);
        ESP_LOGI(TAG,"ATTEMPT %d ready=%d tx=%u samples=%u drops=%u errors=%u",attempt,atomic_load(&s->ready),s->tx_count,s->samples,atomic_load(&s->drops),s->errors);
        vQueueDelete(s->signal);vQueueDelete(s->answer);vQueueDelete(s->control);vQueueDelete(s->tx);vQueueDelete(s->rx);
        for(int i=0;i<4;i++)free(storage[i]);
        vEventGroupDelete(s->done); free(s->pcm); free(s);
        vTaskDelay(pdMS_TO_TICKS(1000)); phase(NULL,"between-sessions");
    }
    free(scratch);free(audio_scratch); ESP_LOGI(TAG,"MATRIX DONE");
}
