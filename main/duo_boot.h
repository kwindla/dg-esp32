#pragma once
#include "app.h"
#include "dg_audio/pipeline.h"
#include "demo_settings.h"
#include <atomic>
#include <mutex>
#include <thread>
namespace dg {
class DuoBoot {
public:
    DuoBoot(App&,dg_audio::Pipeline&,std::atomic<bool>&);
    ~DuoBoot();
    void hold();void resume();void configure(const DemoSettings&);
    void command(const char*,void (*out)(const char*));
    std::string status()const;
    bool ready()const noexcept{return ready_;}
private:
    App& app;dg_audio::Pipeline& audio;std::atomic<bool>& capture;
    std::atomic<bool> quit_{false},held_{false},ready_{false},activated_{false};
    mutable std::mutex gate_;std::thread worker_;
    void run();
};
}
