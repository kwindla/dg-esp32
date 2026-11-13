#include "netprobe.h"
#include "netprobe_core.h"
#include "dg_dump_check.h"
#include "dg_dump_output.h"
#include "dg_board/board.h"
#include <memory>
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/api.h"
#include "lwip/sockets.h"
#include "lwip/stats.h"
#include "lwip/tcpip.h"
#include "lwip/priv/tcpip_priv.h"
#include "lwip/udp.h"
#include "mbedtls/base64.h"
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <optional>
#include <nlohmann/json.hpp>
namespace {
portMUX_TYPE mailbox_mux=portMUX_INITIALIZER_UNLOCKED;
dg::netprobe::MailboxTracker mailboxes;
}
// Track UDP netconn receive mailboxes, including esp_peer's sockets. lwIP does
// not increment udp.drop on this path. Wrappers preserve all original behavior.
extern "C" {
netconn* __real_netconn_new_with_proto_and_callback(netconn_type,u8_t,netconn_callback);
netconn* __wrap_netconn_new_with_proto_and_callback(netconn_type type,u8_t proto,netconn_callback callback) {
    auto* conn=__real_netconn_new_with_proto_and_callback(type,proto,callback);
    if(conn && NETCONNTYPE_GROUP(type)==NETCONN_UDP) {
        portENTER_CRITICAL(&mailbox_mux);
        mailboxes.add(&conn->recvmbox,conn);
        portEXIT_CRITICAL(&mailbox_mux);
    }
    return conn;
}
err_t __real_netconn_delete(netconn*);
err_t __real_netconn_bind(netconn*,const ip_addr_t*,u16_t);
err_t __wrap_netconn_bind(netconn* conn,const ip_addr_t* address,u16_t port) {
    auto result=__real_netconn_bind(conn,address,port);
    if(result==ERR_OK && conn && NETCONNTYPE_GROUP(conn->type)==NETCONN_UDP) {
        portENTER_CRITICAL(&mailbox_mux);
        mailboxes.bind(conn->pcb.udp?conn->pcb.udp->local_port:port,&conn->recvmbox);
        portEXIT_CRITICAL(&mailbox_mux);
    }
    return result;
}
err_t __wrap_netconn_delete(netconn* conn) {
    auto* box=conn?&conn->recvmbox:nullptr;
    portENTER_CRITICAL(&mailbox_mux);
    bool tracked=box && mailboxes.connection(box)==conn;
    uint16_t prior_port=tracked && conn->pcb.udp?conn->pcb.udp->local_port:0;
    if(tracked)mailboxes.remove(box);
    portEXIT_CRITICAL(&mailbox_mux);
    // Remove before lwIP frees the connection: another core can reuse that
    // address immediately. Failed deletion leaves the connection alive.
    auto result=__real_netconn_delete(conn);
    if(result!=ERR_OK && tracked) {
        portENTER_CRITICAL(&mailbox_mux);mailboxes.add(box,conn);mailboxes.bind(prior_port,box);portEXIT_CRITICAL(&mailbox_mux);
    }
    return result;
}
err_t __real_sys_mbox_trypost(sys_mbox_t*,void*);
err_t __wrap_sys_mbox_trypost(sys_mbox_t* box,void* message) {
    auto error=__real_sys_mbox_trypost(box,message);
    if(error!=ERR_OK) {
        portENTER_CRITICAL(&mailbox_mux);
        // UDP delivery executes in tcpip: the PCB port belongs to this socket.
        auto* conn=static_cast<netconn*>(mailboxes.connection(box));
        if(conn)mailboxes.overflow(box,conn->pcb.udp?conn->pcb.udp->local_port:0,esp_timer_get_time());
        portEXIT_CRITICAL(&mailbox_mux);
    }
    return error;
}
}
namespace dg::netprobe {
namespace {
struct Counters {
    uint32_t received=0,unique=0,duplicates=0,outside=0,sent=0,send_errors=0,invalid=0;
    uint64_t max_gap_us=0,max_overrun_us=0,max_send_period_us=0,overwritten=0;
    int64_t last_rx_us=0,last_tx_us=0;
};
struct Path {
    int socket=-1; sockaddr_in remote{};unsigned pps=50,bytes=130,echo_pps=0;
    std::atomic<bool> stop{false},done{true};
    Record* rows=nullptr;uint8_t* bits=nullptr;std::optional<SequenceBitmap> sequence;Counters counters{};
    size_t head=0;int64_t began_us=0,ended_us=0;char host[32]{};
};
Path paths[2];
portMUX_TYPE counters_mux=portMUX_INITIALIZER_UNLOCKED;
std::atomic<bool> task_stop{false},task_done{true};
uint64_t max_loop_gap_us=0;unsigned task_stack_free=0;
void run(void*) {
    uint8_t rx[1232]{},tx[1200]{};int64_t last_loop=0,next[2]{},next_echo[2]{};
    while(!task_stop) {
        auto now=esp_timer_get_time();
        portENTER_CRITICAL(&counters_mux);
        if(last_loop)max_loop_gap_us=std::max(max_loop_gap_us,uint64_t(now-last_loop));
        portEXIT_CRITICAL(&counters_mux);last_loop=now;
        for(unsigned i=0;i<2;++i) {
            auto& p=paths[i];
            if(p.done || p.stop)continue;
            if(!next[i])next[i]=now;
            // Absolute schedule independent of RX; never flood to catch up.
            if(now>=next[i]) {
                uint32_t seq=p.counters.sent;auto sent_at=esp_timer_get_time();
                std::memcpy(tx,"NPU1",4);std::memcpy(tx+4,&seq,4);std::memcpy(tx+8,&sent_at,8);
                int n=sendto(p.socket,tx,p.bytes,MSG_DONTWAIT,reinterpret_cast<sockaddr*>(&p.remote),sizeof p.remote);
                if(n!=int(p.bytes)) {
                    p.rows[p.head%ring_capacity]={sent_at,seq,uint16_t(i),uint16_t(errno?errno:1)};++p.head;
                }
                portENTER_CRITICAL(&counters_mux);
                auto& c=p.counters;++c.sent;
                if(n!=int(p.bytes)) {
                    ++c.send_errors;
                    c.overwritten=p.head>ring_capacity?p.head-ring_capacity:0;
                }
                c.max_overrun_us=std::max(c.max_overrun_us,uint64_t(sent_at-next[i]));
                if(c.last_tx_us)c.max_send_period_us=std::max(c.max_send_period_us,uint64_t(sent_at-c.last_tx_us));
                c.last_tx_us=sent_at;portEXIT_CRITICAL(&counters_mux);
                next[i]+=1000000/p.pps;
                if(next[i]<=sent_at)next[i]=sent_at+1000000/p.pps;
            }
        }
        fd_set readable;FD_ZERO(&readable);int maximum=-1;
        for(auto& p:paths)if(!p.done && !p.stop) {FD_SET(p.socket,&readable);maximum=std::max(maximum,p.socket);}
        timeval timeout{0,1000};
        // Socket-only wait; wake on arrivals, then drain the complete burst.
        int ready=lwip_select(maximum+1,&readable,nullptr,nullptr,&timeout);
        if(ready<=0)continue;
        for(unsigned i=0;i<2;++i) {
            auto& p=paths[i];if(p.done || p.stop || !FD_ISSET(p.socket,&readable))continue;
            for(;;) {
                sockaddr_in from{};socklen_t length=sizeof from;
                int n=recvfrom(p.socket,rx,sizeof rx,MSG_DONTWAIT,reinterpret_cast<sockaddr*>(&from),&length);
                int64_t arrival=esp_timer_get_time();
                if(n<0) {if(errno==EINTR)continue;break;} // EAGAIN: burst fully drained.
                if(n!=int(p.bytes) || std::memcmp(rx,"NPD1",4) || from.sin_addr.s_addr!=p.remote.sin_addr.s_addr || from.sin_port!=p.remote.sin_port) {
                    portENTER_CRITICAL(&counters_mux);++p.counters.invalid;portEXIT_CRITICAL(&counters_mux);continue;
                }
                uint32_t seq;std::memcpy(&seq,rx+4,4);
                auto accepted=p.sequence->add(seq);
                bool outside=accepted==SequenceBitmap::Outside,duplicate=accepted==SequenceBitmap::Duplicate;
                p.rows[p.head%ring_capacity]={arrival,seq,uint16_t(i),0};++p.head;
                portENTER_CRITICAL(&counters_mux);auto& c=p.counters;
                ++c.received;if(outside)++c.outside;else if(duplicate)++c.duplicates;else ++c.unique;
                if(c.last_rx_us)c.max_gap_us=std::max(c.max_gap_us,uint64_t(arrival-c.last_rx_us));
                c.last_rx_us=arrival;c.overwritten=p.head>ring_capacity?p.head-ring_capacity:0;
                portEXIT_CRITICAL(&counters_mux);
                if(p.echo_pps && arrival>=next_echo[i]) {
                    std::memcpy(rx,"NPE1",4);std::memcpy(rx+n,&arrival,8);auto echo_at=esp_timer_get_time();std::memcpy(rx+n+8,&echo_at,8);
                    sendto(p.socket,rx,n+16,MSG_DONTWAIT,reinterpret_cast<sockaddr*>(&p.remote),sizeof p.remote);
                    next_echo[i]=arrival+1000000/p.echo_pps;
                }
            }
        }
    }
    task_stack_free=uxTaskGetStackHighWaterMark(nullptr);
    for(auto& p:paths)if(!p.done) {close(p.socket);p.socket=-1;p.ended_us=esp_timer_get_time();p.done=true;}
    task_done=true;vTaskDelete(nullptr);
}
void stop() {
    task_stop=true;while(!task_done)vTaskDelay(pdMS_TO_TICKS(2));
}
struct StackStats {uint32_t udp_drop=0,udp_memerr=0,ip_drop=0,ip_memerr=0;unsigned counter_bits=16;};
void stack_snapshot(void* arg) {
    [[maybe_unused]] auto& s=*static_cast<StackStats*>(arg);
#if LWIP_STATS
    s.counter_bits=sizeof(STAT_COUNTER)*8;
#endif
#if UDP_STATS
    s.udp_drop=lwip_stats.udp.drop;s.udp_memerr=lwip_stats.udp.memerr;
#endif
#if IP_STATS
    s.ip_drop=lwip_stats.ip.drop;s.ip_memerr=lwip_stats.ip.memerr;
#endif
}

}
void command(const char* args,Write write) {
    char action[16]{};if(std::sscanf(args,"%15s",action)!=1)throw std::runtime_error("netprobe args");
    if(!std::strcmp(action,"start")) {
        // Config mutation happens with task quiescent. For simultaneous paths,
        // configure both endpoints in one start command: host port ... host port.
        if(!task_done)throw std::runtime_error("Stop before start");
        char host[2][32]{};unsigned port[2]{},pps=50,bytes=130,echo=0;
        int fields=std::sscanf(args,"%*s %31s %u %u %u %u %31s %u",host[0],&port[0],&pps,&bytes,&echo,host[1],&port[1]);
        if(fields<2 || (fields==6) || pps<1 || pps>1000 || bytes<16 || bytes>1200 || echo>2 || (echo==2 && fields!=7))throw std::runtime_error("netprobe start host port [pps bytes echo [host2 port2]]; echo 0=off, 1=all, 2=5pps path1 only");
        unsigned count=fields==7?2:1;
        for(unsigned i=0;i<count;++i) {
            auto& p=paths[i];if(!port[i] || port[i]>65535)throw std::runtime_error("probe port");
            if(!p.rows)p.rows=static_cast<Record*>(heap_caps_malloc(sizeof(Record)*ring_capacity,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
            if(!p.bits)p.bits=static_cast<uint8_t*>(heap_caps_malloc(bitmap_capacity/8,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
            if(!p.rows || !p.bits)throw std::bad_alloc();
            p.remote={};p.remote.sin_family=AF_INET;p.remote.sin_port=htons(port[i]);
            if(inet_pton(AF_INET,host[i],&p.remote.sin_addr)!=1)throw std::runtime_error("IPv4 required");
        }
        try {
            for(unsigned i=0;i<2;++i) {
                auto& p=paths[i];p.counters={};p.head=0;p.stop=false;p.done=true;
                if(i>=count)continue;
                p.sequence.emplace(p.bits,bitmap_capacity);std::strcpy(p.host,host[i]);
                p.pps=pps;p.bytes=bytes;p.echo_pps=echo==1?pps:echo==2 && i==1?5:0;p.began_us=esp_timer_get_time();p.ended_us=0;
                p.socket=socket(AF_INET,SOCK_DGRAM,IPPROTO_UDP);if(p.socket<0)throw std::runtime_error("probe socket");
                sockaddr_in local{};local.sin_family=AF_INET;local.sin_port=htons(47000+i);local.sin_addr.s_addr=INADDR_ANY;
                if(bind(p.socket,reinterpret_cast<sockaddr*>(&local),sizeof local))throw std::runtime_error("probe bind");
                portENTER_CRITICAL(&mailbox_mux);mailboxes.bind(47000+i);portEXIT_CRITICAL(&mailbox_mux);
                p.done=false;
            }
            max_loop_gap_us=0;task_stack_free=0;task_stop=false;task_done=false;
            if(xTaskCreatePinnedToCore(run,"netprobe",6144,nullptr,18,nullptr,1)!=pdPASS) {task_done=true;throw std::bad_alloc();}
        } catch(...) {
            for(auto& p:paths)if(p.socket>=0) {close(p.socket);p.socket=-1;p.done=true;}
            throw;
        }
    } else if(!std::strcmp(action,"stop"))stop();
    else if(!std::strcmp(action,"stats")) {
        using nlohmann::json;StackStats stack{};
        struct Message {tcpip_api_call_data base;StackStats* result;};Message msg{};msg.result=&stack;
        auto callback=[](tcpip_api_call_data* call)->err_t {stack_snapshot(reinterpret_cast<Message*>(call)->result);return ERR_OK;};
        if(tcpip_api_call(callback,&msg.base)!=ERR_OK)throw std::runtime_error("lwIP snapshot");
        json result={{"version",1},{"device_us",esp_timer_get_time()},{"running",!task_done.load()},{"priority",18},{"core",1},
            {"ring_capacity_per_path",ring_capacity},{"bitmap_capacity",bitmap_capacity},{"stack_free_bytes",task_stack_free},
            {"lwip_stats_enabled",bool(LWIP_STATS)},{"lwip_counter_bits",stack.counter_bits},{"udp_drop",stack.udp_drop},{"udp_memerr",stack.udp_memerr},{"ip_drop",stack.ip_drop},{"ip_memerr",stack.ip_memerr}};
        portENTER_CRITICAL(&mailbox_mux);auto tracked=mailboxes;portEXIT_CRITICAL(&mailbox_mux);
        result["udp_recvmbox_full"]=tracked.full;result["udp_recvmbox_last_us"]=tracked.last_us;result["udp_mailbox_untracked"]=tracked.untracked;
        result["udp_mailbox_untracked_ports"]=tracked.untracked_ports;result["udp_recvmbox_ports"]=json::array();
        for(auto& entry:tracked.ports)if(entry.used)result["udp_recvmbox_ports"].push_back({{"local_port",entry.local_port},{"full",entry.full},{"last_us",entry.last_us}});
        Counters copy[2];uint64_t gap;
        portENTER_CRITICAL(&counters_mux);for(unsigned i=0;i<2;++i)copy[i]=paths[i].counters;gap=max_loop_gap_us;portEXIT_CRITICAL(&counters_mux);
        result["max_loop_gap_us"]=gap;result["paths"]=json::array();
        for(unsigned i=0;i<2;++i) {
            auto& p=paths[i];auto& c=copy[i];
            result["paths"].push_back({{"path",i},{"host",p.host},{"pps",p.pps},{"bytes",p.bytes},{"echo_pps",p.echo_pps},{"local_port",47000+i},{"timestamp_boundary","socket drain"},{"began_us",p.began_us},{"ended_us",p.ended_us},
                {"sent",c.sent},{"send_errors",c.send_errors},{"received",c.received},{"unique",c.unique},{"duplicates",c.duplicates},{"outside",c.outside},
                {"invalid",c.invalid},{"overwritten",c.overwritten},{"max_gap_us",c.max_gap_us},{"max_send_overrun_us",c.max_overrun_us},{"max_send_period_us",c.max_send_period_us}});
        }
        wifi_ap_record_t ap{};
        if(esp_wifi_sta_get_ap_info(&ap)==ESP_OK) {
            char bssid[18];std::snprintf(bssid,sizeof bssid,"%02x:%02x:%02x:%02x:%02x:%02x",ap.bssid[0],ap.bssid[1],ap.bssid[2],ap.bssid[3],ap.bssid[4],ap.bssid[5]);
            result["wifi"]={{"rssi_dbm",ap.rssi},{"channel",ap.primary},{"bssid",bssid},{"phy_rate_mbps",nullptr},
                {"phy_rate_note","Current negotiated PHY rate unavailable through public station API; see wifi-stats dump"},{"phy_11n",bool(ap.phy_11n)}};
        }
        write(("\nNETPROBE "+result.dump()+"\n").c_str());
    } else if(!std::strcmp(action,"dump")) {
        if(!task_done)throw std::runtime_error("Stop before dump");
        const bool uart=dg_board::info().console==dg_board::ConsoleKind::Uart;
        std::unique_ptr<dg_console::DumpCheck> check;
        if(uart)check=std::make_unique<dg_console::DumpCheck>();
        dg_console::dump_line("\nNETPROBE_DUMP BEGIN version=1 record_bytes=16\n",write);
        for(auto& p:paths) {
            size_t first=p.head>ring_capacity?p.head-ring_capacity:0;
            for(size_t at=first;at<p.head;at+=24) {
                Record rows[24];size_t count=std::min(size_t(24),p.head-at);
                for(size_t j=0;j<count;++j)rows[j]=p.rows[(at+j)%ring_capacity];
                unsigned char encoded[513];size_t length=0;
                mbedtls_base64_encode(encoded,sizeof encoded,&length,reinterpret_cast<unsigned char*>(rows),count*sizeof(Record));
                char line[560];std::snprintf(line,sizeof line,"\nNETPROBE_ROW %.*s\n",int(length),encoded);
                if(check)check->record(line);
                // Four milliseconds preserves USB wire bytes and bounds its
                // previously unpaced burst; UART uses the measured wire rate.
                dg_console::dump_line(line,write,4);
            }
        }
        if(check){char text[128];check->finish("NETPROBE_DUMP",text,sizeof text);dg_console::dump_line(text,write);}
        dg_console::dump_line("\nNETPROBE_DUMP END\n",write);
    } else throw std::runtime_error("netprobe action");
}
}
