#pragma once
#include "app.h"
#include "dg_audio/pipeline.h"
#include "demo_settings.h"
#include "demo_intent.h"
#include <atomic>
#include <thread>
namespace dg {
class DemoBoot {
public:
    DemoBoot(App&,dg_audio::Pipeline&,std::atomic<bool>& capture);
    ~DemoBoot();
    void hold();
    void resume();
    void configure(const DemoSettings&);
    std::string status() const;
private:
    App& app;dg_audio::Pipeline& audio;std::atomic<bool>& capture;
    std::atomic<bool> quit{false},initializing{false};
    DemoIntent intent;
    std::atomic<unsigned> attempts{0},failures{0};
    std::thread worker;
    void run();
};
}
