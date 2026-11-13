#include "app_wait.h"
#include <gtest/gtest.h>
#include <atomic>
#include <future>
#include <thread>
using namespace std::chrono_literals;

TEST(AppWaitTest, ChangeBeforeWaitIsVisibleInThePredicate) {
    AppWait wait;
    std::atomic<bool> ready{true};wait.changed();
    EXPECT_TRUE(wait.wait(100ms,[&]{return ready.load();}));
}

TEST(AppWaitTest, NotificationCannotPassAPredicateThatIsAboutToSleep) {
    AppWait wait;std::atomic<bool> ready{false};
    std::promise<void> checked,release_predicate,notified,finished;
    auto release=release_predicate.get_future().share();
    auto notify_done=notified.get_future();auto wait_done=finished.get_future();
    bool result=false;
    std::thread waiter([&]{
        bool first=true;
        result=wait.wait(2s,[&]{
            bool snapshot=ready.load();
            if(first) {first=false;checked.set_value();release.wait();}
            return snapshot;
        });
        finished.set_value();
    });
    checked.get_future().wait();ready=true;
    std::thread notifier([&]{wait.changed();notified.set_value();});
    // The predicate still holds the wait mutex. Without the notifier lock,
    // notification happens here, then the waiter sleeps until its deadline.
    EXPECT_EQ(notify_done.wait_for(50ms),std::future_status::timeout);
    release_predicate.set_value();
    EXPECT_EQ(wait_done.wait_for(500ms),std::future_status::ready);
    notifier.join();waiter.join();EXPECT_TRUE(result);
}
