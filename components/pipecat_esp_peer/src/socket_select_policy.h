#pragma once
#include <sys/select.h>
namespace pipecat::esp_peer {
inline bool sockets_only(int nfds,const fd_set* read,const fd_set* write,const fd_set* error,int offset) {
    if(nfds<=0 || nfds>FD_SETSIZE)return false;
    bool any=false;
    for(int fd=0;fd<nfds;++fd) {
        if((read && FD_ISSET(fd,read)) || (write && FD_ISSET(fd,write)) || (error && FD_ISSET(fd,error))) {
            if(fd<offset)return false;
            any=true;
        }
    }
    return any;
}
}
