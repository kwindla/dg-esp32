#pragma once
#include <algorithm>
#include <cstddef>
namespace dg_console {
enum class Admission {Queued, Full};
template<class Enqueue>
Admission enqueue_record(const char* text,size_t bytes,Enqueue enqueue) {
    return enqueue(text,bytes)?Admission::Queued:Admission::Full;
}
// Only the console drain task retries. Each driver attempt has zero wait.
// SOF presence is advisory: it can briefly read false with an attached reader,
// or remain true with no reader. Neither value may discard an otherwise
// deliverable command ACK. A fixed deadline handles genuine backpressure.
template<class Send, class Clock, class Yield>
bool deliver(const char* text, size_t size, Send send, Clock clock, Yield yield) {
    const auto deadline=clock()+1000000;
    while(size) {
        if(clock()>=deadline)return false;
        const auto chunk=std::min(size,size_t(2048));
        const int sent=send(text,chunk);
        if(sent>0 && size_t(sent)<=chunk){text+=sent;size-=sent;}
        else yield();
    }
    return true;
}
}
