#pragma once
#include <cstdint>
namespace dg_board {
struct TouchCounts {uint32_t taps=0,double_taps=0,ignored=0,rejected=0;};
// Single owner. Two distinct, short contacts; stale/bounced/dragged contacts
// cannot become taps. Unsigned elapsed time survives the millisecond wrap.
class DoubleTap {
public:
    bool sample(uint32_t now,bool down,uint16_t x,uint16_t y,bool enabled) noexcept {
        if(enabled!=enabled_){pending_=false;valid_=false;enabled_=enabled;}
        if(down){
            if(!down_){down_=true;began_=now;x_=x;y_=y;valid_=enabled;}
            if(distance(x,x_)>30 || distance(y,y_)>30 || now-began_>250)valid_=false;
            return false;
        }
        if(!down_)return false;
        down_=false;
        if(!enabled){++counts_.ignored;pending_=false;return false;}
        if(!valid_ || now-began_<20 || now-began_>250){++counts_.rejected;pending_=false;return false;}
        ++counts_.taps;
        const uint32_t elapsed=now-first_;
        if(pending_ && elapsed>=80 && elapsed<400 && distance(x_,first_x_)<=60 && distance(y_,first_y_)<=60){
            pending_=false;++counts_.double_taps;return true;
        }
        if(pending_ && elapsed<80){++counts_.rejected;pending_=false;return false;}
        pending_=true;first_=now;first_x_=x_;first_y_=y_;return false;
    }
    void cancel() noexcept {pending_=false;valid_=false;}
    TouchCounts counts()const noexcept{return counts_;}
private:
    static unsigned distance(unsigned a,unsigned b){return a>b?a-b:b-a;}
    TouchCounts counts_;
    bool down_=false,valid_=false,pending_=false,enabled_=false;
    uint32_t began_=0,first_=0;uint16_t x_=0,y_=0,first_x_=0,first_y_=0;
};
}
