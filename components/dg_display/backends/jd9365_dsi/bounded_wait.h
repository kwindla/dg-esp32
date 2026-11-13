#pragma once
#include <cstdint>

namespace panel_probe {
// One deadline is shared by all phases of a DSI read. Pause must block the
// calling task (taskYIELD alone would not schedule lower-priority IDLE0).
template<class Ready,class Clock,class Pause>
bool wait_ready(Ready ready,Clock clock,Pause pause,int64_t deadline) {
    while(!ready()) {
        if(clock()>=deadline)return false;
        pause();
    }
    return true;
}
}
