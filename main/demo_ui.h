#pragma once
#include "app.h"
#include <atomic>
#include <functional>
#include <thread>
namespace dg {
class DemoUi {
public:
    ~DemoUi(){stop();}
    void start(std::function<bool()> request_reconnect);
    void stop();
    void lifecycle(DemoPhase phase,uint32_t generation)noexcept;
    void command(const char* command,unsigned argument,void(*out)(const char*));
private:
    std::function<bool()> request_;
    std::thread worker_;
    std::atomic<bool> quit_{false},enabled_{true},pending_{false};
    std::atomic<DemoPhase> phase_{DemoPhase::Disconnected};
    std::atomic<uint32_t> generation_{0},requests_{0},request_rejected_{0},phase_ignored_{0};
};
}
