#pragma once
#include <algorithm>
#include <chrono>
#include <cstdint>
namespace pipecat::esp_peer {
// The deadline one connect runs under. Public HTTPS signalling pays two TLS
// handshakes (/start and the offer), so it gets at least 40 s; a LAN connect
// keeps the configured time. The injected-fault bound wins over both, so a
// fault-injection run fails in 3 s on either kind of endpoint.
inline std::chrono::milliseconds connect_deadline(std::chrono::milliseconds configured,bool https,bool injected_fault) {
    if(injected_fault)return std::chrono::milliseconds(3000);
    return https?std::max(configured,std::chrono::milliseconds(40000)):configured;
}
inline bool handshake_cancelled(bool stopping,int64_t now,int64_t deadline) {
    return stopping || (deadline && now>=deadline);
}
inline bool handshake_terminal(int result,int want_read,int want_write,int hello_verify) {
    return result && result!=want_read && result!=want_write && result!=hello_verify;
}
}
