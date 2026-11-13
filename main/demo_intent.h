#pragma once
#include <atomic>
namespace dg {
struct DemoIntent {
    std::atomic<bool> held{false},activated{false};
    void hold() {held=true;activated=false;}
    void resume() {activated=false;held=false;}
};
}
