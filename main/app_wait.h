#pragma once
#include <chrono>
#include <condition_variable>
#include <mutex>

// State predicates use atomics. After changing them, take the wait mutex before
// notifying so a change cannot be lost between predicate evaluation and sleep.
class AppWait {
    std::mutex mutex_;
    std::condition_variable changed_;
public:
    void changed() {
        std::lock_guard<std::mutex> lock(mutex_);
        changed_.notify_all();
    }
    template<class Predicate> bool wait(std::chrono::milliseconds timeout,Predicate done) {
        std::unique_lock<std::mutex> lock(mutex_);
        return changed_.wait_for(lock,timeout,done);
    }
};
