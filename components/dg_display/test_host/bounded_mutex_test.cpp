#include <gtest/gtest.h>
#include "dg_display/bounded_mutex.h"
#include "dg_display/connection_scope.h"
#include <stdexcept>
#include <type_traits>
using namespace dg_display;
namespace {
struct Mutex {
    unsigned calls=0,misses=0,failures=0,gives=0,waited=0,success_on=0,progress=0;
    bool stopped=false,stop_after_take=false,progress_each_miss=false;
    bool terminal()const noexcept{return stopped;}
    bool take(unsigned ms)noexcept{
        ++calls;waited+=ms;
        if(stop_after_take)stopped=true;
        return calls==success_on;
    }
    void give()noexcept{++gives;}
    void miss(bool exhausted)noexcept{
        ++misses;if(progress_each_miss)++progress;
        if(exhausted){++failures;stopped=true;}
    }
};
}
TEST(EngineMutex,ImmediateSuccessOwnsAndReleasesExactlyOnce){
    Mutex mutex;mutex.success_on=1;
    {EngineMutexLease lease(mutex);EXPECT_TRUE(lease);EXPECT_EQ(mutex.gives,0u);}
    EXPECT_EQ(mutex.calls,1u);EXPECT_EQ(mutex.misses,0u);EXPECT_EQ(mutex.gives,1u);
}
TEST(EngineMutex,TransientMissRecoversWithoutTerminalState){
    Mutex mutex;mutex.success_on=3;
    {EngineMutexLease lease(mutex);EXPECT_TRUE(lease);}
    EXPECT_EQ(mutex.calls,3u);EXPECT_EQ(mutex.misses,2u);EXPECT_EQ(mutex.failures,0u);
    EXPECT_FALSE(mutex.stopped);EXPECT_EQ(mutex.gives,1u);
}
TEST(EngineMutex,PermanentlyHeldMutexHasFiniteWaitAndNeverEntersCriticalSection){
    Mutex mutex;bool entered=false;
    {EngineMutexLease lease(mutex);if(lease)entered=true;}
    EXPECT_FALSE(entered);EXPECT_EQ(mutex.calls,3u);EXPECT_EQ(mutex.waited,6000u);
    EXPECT_EQ(mutex.misses,3u);EXPECT_EQ(mutex.failures,1u);EXPECT_TRUE(mutex.stopped);
    EXPECT_EQ(mutex.gives,0u); // No token was acquired.
    {EngineMutexLease lease(mutex);EXPECT_FALSE(lease);}
    EXPECT_EQ(mutex.calls,3u);EXPECT_EQ(mutex.failures,1u);EXPECT_EQ(mutex.gives,0u);
}
TEST(EngineMutex,ProgressElsewhereCannotExtendWaitBudget){
    Mutex mutex;mutex.progress_each_miss=true;
    EXPECT_FALSE(acquire_engine_mutex(mutex));
    EXPECT_EQ(mutex.progress,3u);EXPECT_EQ(mutex.calls,3u);EXPECT_EQ(mutex.waited,6000u);
    EXPECT_TRUE(mutex.stopped);
}
TEST(EngineMutex,TerminalRaceAfterSuccessfulTakeReturnsTokenWithoutEntering){
    Mutex mutex;mutex.success_on=1;mutex.stop_after_take=true;
    {EngineMutexLease lease(mutex);EXPECT_FALSE(lease);}
    EXPECT_EQ(mutex.calls,1u);EXPECT_EQ(mutex.gives,1u);EXPECT_EQ(mutex.misses,0u);
}
TEST(EngineMutex,ExistingTerminalStateNeverWaitsOrReleasesAnotherOwnersToken){
    Mutex mutex;mutex.stopped=true;
    {EngineMutexLease lease(mutex);EXPECT_FALSE(lease);}
    EXPECT_EQ(mutex.calls,0u);EXPECT_EQ(mutex.gives,0u);EXPECT_EQ(mutex.failures,0u);
}
TEST(EngineMutex,LeaseReleasesDuringExceptionUnwind){
    Mutex mutex;mutex.success_on=1;
    try{EngineMutexLease lease(mutex);ASSERT_TRUE(lease);throw std::runtime_error("operation failed");}
    catch(const std::runtime_error&){}
    EXPECT_EQ(mutex.gives,1u);
    static_assert(!std::is_copy_constructible_v<EngineMutexLease<Mutex>>);
}

TEST(EngineMutex,TimeoutExceptionConstructionAndCopyCannotAllocate){
    static_assert(std::is_nothrow_default_constructible_v<EngineLockTimeout>);
    static_assert(std::is_nothrow_copy_constructible_v<EngineLockTimeout>);
    EngineLockTimeout error;EXPECT_STREQ(error.what(),"display engine mutex unavailable; terminal; reboot required");
}
TEST(EngineMutex,ConnectionClosesOnLockTimeoutWithoutUnlockingUnownedMutex){
    Mutex mutex;unsigned closes=0;
    try{
        ConnectionScope connection(42,[&](int handle){EXPECT_EQ(handle,42);++closes;});
        EngineMutexLease lease(mutex);if(!lease)throw EngineLockTimeout{};
        FAIL()<<"Critical section entered without ownership";
    }catch(const EngineLockTimeout&){}
    EXPECT_EQ(closes,1u);EXPECT_EQ(mutex.gives,0u);EXPECT_TRUE(mutex.stopped);
}
TEST(EngineMutex,ExplicitConnectionCloseDoesNotCloseTwice){
    unsigned closes=0;
    {ConnectionScope connection(42,[&](int){++closes;});connection.close();connection.close();}
    EXPECT_EQ(closes,1u);
}
