#pragma once
#include <cstdint>
#include <cstddef>
namespace pipecat::esp_peer {
struct DtlsRecordIdentity {uint16_t epoch=0;uint64_t sequence=0;bool complete=false;};
inline bool dtls_application(const void* data,size_t bytes,DtlsRecordIdentity& id) {
    id={};auto* p=static_cast<const uint8_t*>(data);
    if(!bytes || p[0]!=0x17)return false;
    if(bytes>=13) {
        id.epoch=uint16_t(p[3])<<8|p[4];
        for(unsigned i=5;i<11;++i)id.sequence=(id.sequence<<8)|p[i];
        id.complete=true;
    }
    return true;
}
}
