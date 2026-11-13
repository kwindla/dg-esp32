// Independent P4/C6 SDIO spike; no product component or server changes.
#include "console.h"
#include "sdkconfig.h"
#if CONFIG_PM_SLEEP_CLK_ICG_ENABLE
#error "p4_net requires PM_SLEEP_CLK_ICG_ENABLE=n; regenerate build/sdkconfig from defaults"
#endif
#include "esp_wifi.h"
#include "esp_hosted.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "mbedtls/base64.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <vector>
// A weak reference lets the image report absence instead of manufacturing a
// private API implementation. Do not register a callback when it is absent.
extern "C" esp_err_t esp_wifi_set_tx_done_cb(void (*)(uint8_t, uint8_t*, uint16_t*, bool)) __attribute__((weak));
namespace {
EventGroupHandle_t connected;
unsigned reconnects = 0;
std::atomic<bool> busy{false}, stop{false};
struct Job { sockaddr_in remote{}; unsigned seconds=60, pps=50, bytes=130; bool tcp=false; } job;
void rc(const char* name, esp_err_t result) { probe::print("API %s rc=%d name=%s\n", name, result, esp_err_to_name(result)); }
void event(void*, esp_event_base_t base, int32_t id, void* data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) rc("connect", esp_wifi_connect());
    else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        auto* e = static_cast<wifi_event_sta_disconnected_t*>(data);
        probe::print("WIFI disconnected reason=%u retry=%u\n", e->reason, reconnects);
        if (reconnects++ < 10) esp_wifi_connect();
        xEventGroupClearBits(connected, 1);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        auto* e = static_cast<ip_event_got_ip_t*>(data);
        probe::print("WIFI IP=" IPSTR "\n", IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(connected, 1);
    }
}
void status() {
    wifi_ap_record_t ap{};
    auto result = esp_wifi_sta_get_ap_info(&ap); rc("sta_get_ap_info", result);
    if (result == ESP_OK) probe::print("AP bssid=%02x:%02x:%02x:%02x:%02x:%02x rssi=%d primary=%u secondary=%u authmode=%d pairwise=%d group=%d phy_11b=%u phy_11g=%u phy_11n=%u phy_11ax=%u\n",
        ap.bssid[0],ap.bssid[1],ap.bssid[2],ap.bssid[3],ap.bssid[4],ap.bssid[5],
        ap.rssi,ap.primary,unsigned(ap.second),ap.authmode,ap.pairwise_cipher,ap.group_cipher,
        ap.phy_11b,ap.phy_11g,ap.phy_11n,ap.phy_11ax);
    rc("statis_dump", esp_wifi_statis_dump(0xffffffff));
    rc("set_ps_none", esp_wifi_set_ps(WIFI_PS_NONE));
    if (esp_wifi_set_tx_done_cb) rc("set_tx_done_cb", esp_wifi_set_tx_done_cb(nullptr));
    else probe::print("API set_tx_done_cb not linkable\n");
}
struct Row { int64_t arrival; uint32_t seq; uint16_t path, error; };
static_assert(sizeof(Row)==16);
std::vector<Row> rows;
unsigned udp_sent=0, udp_errors=0, overwritten=0, outside=0;
int64_t began_us=0, ended_us=0;
void retain(Row row) { if(rows.size()<16000)rows.push_back(row);else ++overwritten; }
void udp_run(int sock) {
    // hil/netprobe.py wire v1: <4sIQ> LE, NPU1 independent uplink;
    // echo NPD1 verbatim as NPE1 plus device <QQ> receive/send microseconds.
    std::vector<uint8_t> rx(1232), tx(job.bytes), seen(65536 / 8);
    rows.clear();rows.reserve(16000);overwritten=0;outside=0;
    uint32_t sent=0, received=0, unique=0, duplicates=0, invalid=0, send_errors=0, echo_send_errors=0;
    int64_t start=esp_timer_get_time(), next=start, max_gap=0, prior=0;
    began_us=start;
    auto end=start+int64_t(job.seconds)*1000000;
    while (!stop && esp_timer_get_time()<end) {
        auto now=esp_timer_get_time();
        if (now>=next) {
            memcpy(tx.data(),"NPU1",4);memcpy(tx.data()+4,&sent,4);memcpy(tx.data()+8,&now,8);
            if (sendto(sock,tx.data(),tx.size(),MSG_DONTWAIT,reinterpret_cast<sockaddr*>(&job.remote),sizeof job.remote)!=int(tx.size())) {
                ++send_errors;retain({now,sent,0,uint16_t(errno?errno:1)});
            }
            ++sent;next+=1000000/job.pps;if(next<=now)next=now+1000000/job.pps;
        }
        fd_set fds;FD_ZERO(&fds);FD_SET(sock,&fds);timeval timeout{0,1000};
        if (select(sock+1,&fds,nullptr,nullptr,&timeout)<=0) continue;
        for (;;) {
            sockaddr_in from{};socklen_t len=sizeof from;
            int n=recvfrom(sock,rx.data(),rx.size(),MSG_DONTWAIT,reinterpret_cast<sockaddr*>(&from),&len);
            auto arrival=esp_timer_get_time();
            if(n<0)break;
            if(n!=int(job.bytes)||memcmp(rx.data(),"NPD1",4)||from.sin_port!=job.remote.sin_port||from.sin_addr.s_addr!=job.remote.sin_addr.s_addr) {++invalid;continue;}
            uint32_t seq;memcpy(&seq,rx.data()+4,4);++received;retain({arrival,seq,0,0});
            if(seq<65536) {auto& byte=seen[seq/8];uint8_t mask=1u<<(seq%8);if(byte&mask)++duplicates;else{byte|=mask;++unique;}}else ++outside;
            if(prior)max_gap=std::max(max_gap,arrival-prior);
            prior=arrival;
            memcpy(rx.data(),"NPE1",4);memcpy(rx.data()+n,&arrival,8);
            auto echo=esp_timer_get_time();memcpy(rx.data()+n+8,&echo,8);
            if(sendto(sock,rx.data(),n+16,MSG_DONTWAIT,reinterpret_cast<sockaddr*>(&from),len)!=n+16)++echo_send_errors;
        }
    }
    ended_us=esp_timer_get_time();udp_sent=sent;udp_errors=send_errors;
    probe::print("UDP END sent=%lu received=%lu unique=%lu duplicates=%lu invalid=%lu send_errors=%lu echo_send_errors=%lu max_rx_gap_us=%lld elapsed_us=%lld\n",
        (unsigned long)sent,(unsigned long)received,(unsigned long)unique,(unsigned long)duplicates,(unsigned long)invalid,(unsigned long)send_errors,(unsigned long)echo_send_errors,
        (long long)max_gap,(long long)(esp_timer_get_time()-start));
}
void tcp_run(int sock) {
    timeval limit{2,0};setsockopt(sock,SOL_SOCKET,SO_SNDTIMEO,&limit,sizeof limit);
    auto start=esp_timer_get_time();uint64_t bytes=0;int error=0;
    if (connect(sock,reinterpret_cast<sockaddr*>(&job.remote),sizeof job.remote)) error=errno;
    uint8_t data[4096]{};
    while(!error && !stop && esp_timer_get_time()-start<int64_t(job.seconds)*1000000) {
        int n=send(sock,data,sizeof data,0);
        if(n<=0){error=errno?errno:EIO;break;}bytes+=n;
    }
    auto elapsed=esp_timer_get_time()-start;
    probe::print("TCP END bytes=%llu elapsed_us=%lld bits_per_second=%llu errno=%d\n",(unsigned long long)bytes,
        (long long)elapsed,(unsigned long long)(bytes*8000000/std::max<int64_t>(1,elapsed)),error);
}
void run(void*) {
    int sock=socket(AF_INET,job.tcp?SOCK_STREAM:SOCK_DGRAM,job.tcp?IPPROTO_TCP:IPPROTO_UDP);
    if(sock<0)probe::print("ERR socket errno=%d\n",errno);
    else {if(job.tcp)tcp_run(sock);else udp_run(sock);close(sock);}
    busy=false;vTaskDelete(nullptr);
}
void command(char* line) {
    if (!strcmp(line,"status")) status();
    else if (!strcmp(line,"stop")) {stop=true;while(busy)vTaskDelay(1);}
    else if (!strcmp(line,"netstats") && !busy) {
        probe::print("NETPROBE {\"paths\":[{\"sent\":%u,\"send_errors\":%u,\"began_us\":%lld,\"ended_us\":%lld,\"overwritten\":%u,\"outside\":%u}]}\n",
            udp_sent,udp_errors,(long long)began_us,(long long)ended_us,overwritten,outside);
    } else if (!strcmp(line,"netdump") && !busy) {
        probe::print("NETPROBE_DUMP BEGIN record_bytes=16\n");
        for(size_t i=0;i<rows.size();i+=24){
            unsigned char encoded[513];size_t n=0;
            mbedtls_base64_encode(encoded,sizeof encoded,&n,reinterpret_cast<const unsigned char*>(rows.data()+i),std::min(size_t(24),rows.size()-i)*sizeof(Row));
            probe::print("NETPROBE_ROW %.*s crc=%08lx\n",int(n),encoded,(unsigned long)probe::crc32(encoded,n));
        }
        probe::print("NETPROBE_DUMP END records=%u\n",unsigned(rows.size()));
    }
    else if (!strcmp(line,"uptime")) probe::print("UPTIME us=%lld\n",(long long)esp_timer_get_time());
    else {
        char name[16]{},host[32]{};unsigned port=0,seconds=60,pps=50,bytes=130;
        int n=sscanf(line,"%15s %31s %u %u %u %u",name,host,&port,&seconds,&pps,&bytes);
        if(n<4 || (strcmp(name,"udp")&&strcmp(name,"tcp")) || busy || !seconds || seconds>120 ||
           !port || port>65535 || port==7860 || port==7861 || !pps || pps>1000 || bytes<16 || bytes>1200) {
            probe::print("ERR udp|tcp <IPv4> <port> <seconds> [pps bytes]; stop; status\n");return;
        }
        job={};job.remote.sin_family=AF_INET;job.remote.sin_port=htons(port);
        if(inet_pton(AF_INET,host,&job.remote.sin_addr)!=1){probe::print("ERR IPv4\n");return;}
        job.tcp=!strcmp(name,"tcp");job.seconds=seconds;job.pps=pps;job.bytes=bytes;
        stop=false;busy=true;
        if(xTaskCreatePinnedToCore(run,"net-probe",8192,nullptr,10,nullptr,1)!=pdPASS){busy=false;probe::print("ERR task\n");}
        else probe::print("NET START protocol=%s seconds=%u port=%u pps=%u bytes=%u\n",name,seconds,port,pps,bytes);
    }
}
}
extern "C" void app_main() {
    probe::console_init();probe::print("P4_NET boot\n");
    auto nvs=nvs_flash_init();
    if(nvs==ESP_ERR_NVS_NO_FREE_PAGES||nvs==ESP_ERR_NVS_NEW_VERSION_FOUND){ESP_ERROR_CHECK(nvs_flash_erase());nvs=nvs_flash_init();}
    ESP_ERROR_CHECK(nvs);ESP_ERROR_CHECK(esp_netif_init());ESP_ERROR_CHECK(esp_event_loop_create_default());
    connected=xEventGroupCreate();ESP_ERROR_CHECK(connected?ESP_OK:ESP_ERR_NO_MEM);
    esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,event,nullptr));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,event,nullptr));
    wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT();ESP_ERROR_CHECK(esp_wifi_init(&init));
    esp_hosted_coprocessor_fwver_t version{};
    int version_rc=esp_hosted_get_coprocessor_fwversion(&version);
    probe::print("HOSTED FW rpc_rc=%d version=%lu.%lu.%lu revision=%ld prerelease=%ld build=%ld\n",version_rc,
        (unsigned long)version.major1,(unsigned long)version.minor1,(unsigned long)version.patch1,
        (long)version.revision,(long)version.prerelease,(long)version.build);
    wifi_config_t config{};
    strlcpy(reinterpret_cast<char*>(config.sta.ssid),CONFIG_DG_WIFI_SSID,sizeof config.sta.ssid);
    strlcpy(reinterpret_cast<char*>(config.sta.password),CONFIG_DG_WIFI_PASSWORD,sizeof config.sta.password);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA,&config));
    ESP_ERROR_CHECK(esp_wifi_start());
    if(!(xEventGroupWaitBits(connected,1,pdFALSE,pdTRUE,pdMS_TO_TICKS(30000))&1)){probe::print("P4_NET FAIL join timeout\n");return;}
    status();probe::print("P4_NET READY uptime_us=%lld\n",(long long)esp_timer_get_time());
    char line[160]{};size_t used=0;
    for(;;){char input[128];int n=probe::read(input,sizeof input);for(int i=0;i<n;++i){
        char c=input[i];if(c=='\r')continue;
        if(c=='\n'){line[used]=0;if(used){command(line);probe::print("OK\n");}used=0;}
        else if(used+1<sizeof line)line[used++]=c;
    }}
}
