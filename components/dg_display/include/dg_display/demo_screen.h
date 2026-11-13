#pragma once
#include <cstdint>
namespace dg_display {
enum class DemoScreen : unsigned {Disconnected,Connecting,Live};
struct DemoFrame {DemoScreen screen=DemoScreen::Disconnected;unsigned dots=0;};
// Pure scheduling: no catch-up bursts after stalls; each state starts at dot0.
class DemoAnimation {
public:
    DemoFrame tick(DemoScreen requested,int64_t now_ms) noexcept {
        if(!started_ || requested!=frame_.screen){started_=true;frame_={requested,0};next_=now_ms+350;}
        else if(requested==DemoScreen::Connecting && now_ms>=next_){frame_.dots=(frame_.dots+1)%3;next_=now_ms+350;}
        return frame_;
    }
private:
    bool started_=false;int64_t next_=0;DemoFrame frame_;
};
// Nonblocking app hook. Enable rendering with `disp demo on` after all cat and
// demo_assets[] hashes have been preloaded/persisted. Live only on bot-ready.
void demo_screen(DemoScreen screen) noexcept;
}
