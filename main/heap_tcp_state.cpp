#include "heap_tcp_state.h"
#include "lwip/tcpip.h"
#include "lwip/priv/tcpip_priv.h"
#include "lwip/priv/tcp_priv.h"
#include <cstdio>
#include <stdexcept>

namespace dg { namespace {
struct Request {tcpip_api_call_data call;unsigned active=0,time_wait=0,bound=0;};
bool count(const tcp_pcb* pcb,unsigned& n){for(;pcb;pcb=pcb->next)if(++n>1024)return false;return true;}
err_t snapshot(tcpip_api_call_data* data){
    auto* r=reinterpret_cast<Request*>(data);
    return count(tcp_active_pcbs,r->active) && count(tcp_tw_pcbs,r->time_wait) && count(tcp_bound_pcbs,r->bound)?ERR_OK:ERR_VAL;
}
}
void heap_tcp_state(unsigned window,void (*out)(const char*)){
    Request request{};
    if(tcpip_api_call(snapshot,&request.call)!=ERR_OK)throw std::runtime_error("TCP state census failed");
    char line[320];std::snprintf(line,sizeof line,"\nHEAP_ATTRIBUTION {\"schema\":1,\"kind\":\"tcp\",\"id\":%u,\"active\":%u,\"time_wait\":%u,\"bound\":%u,\"tcp_msl_ms\":%u,\"time_wait_ms\":%u,\"memp_num_tcp_pcb\":%u}\n",window,request.active,request.time_wait,request.bound,unsigned(TCP_MSL),unsigned(2*TCP_MSL),unsigned(MEMP_NUM_TCP_PCB));out(line);
}
}
