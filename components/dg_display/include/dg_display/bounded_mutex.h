#pragma once
#include <exception>
namespace dg_display {
// No string allocation while reporting a mutex failure under heap pressure.
struct EngineLockTimeout final:std::exception {
    EngineLockTimeout()noexcept=default;
    const char* what()const noexcept override{return "display engine mutex unavailable; terminal; reboot required";}
};
// Total semaphore-wait budget per acquisition; scheduler descheduling can add
// wall time. Progress elsewhere never resets this finite attempt count.
inline constexpr unsigned EngineLockWaitMs=2000,EngineLockAttempts=3;
// Backend owns terminal state/counters; no protected state is touched here.
template<class Backend> bool acquire_engine_mutex(Backend& backend) noexcept {
    for(unsigned attempt=0;attempt<EngineLockAttempts;++attempt){
        if(backend.terminal())return false;
        if(backend.take(EngineLockWaitMs)){
            if(!backend.terminal())return true;
            backend.give();return false; // Another waiter declared terminal.
        }
        backend.miss(attempt+1==EngineLockAttempts);
    }
    return false;
}
template<class Backend> class EngineMutexLease {
public:
    explicit EngineMutexLease(Backend& backend) noexcept:backend_(backend),held_(acquire_engine_mutex(backend)){}
    ~EngineMutexLease(){if(held_)backend_.give();}
    EngineMutexLease(const EngineMutexLease&)=delete;
    EngineMutexLease& operator=(const EngineMutexLease&)=delete;
    explicit operator bool()const noexcept{return held_;}
private:
    Backend& backend_;bool held_;
};
}
