#pragma once
#include <atomic>
#include <chrono>
#include <thread>
namespace boot_fake {
inline std::atomic<bool> session{false}, idle{true};
inline std::atomic<unsigned> requests{0}, ended{0};
inline std::atomic<bool> pause{false}, paused{false};
}
namespace dg {class App {public:
 bool idle() {return boot_fake::idle;}
 void server_url(const char*) {}
 void request_connect() {boot_fake::idle=false;boot_fake::session=true;++boot_fake::requests;}
 void disconnect() {std::this_thread::sleep_for(std::chrono::milliseconds(5));boot_fake::idle=true;}
};}
