#include "heap_new_callers.h"
#include "freertos/FreeRTOS.h"
#include <gtest/gtest.h>
#include <cstdlib>
#include <new>

namespace {bool fail=false;
template<class... T>void* allocate(std::size_t n,const T&...){if(fail)throw std::bad_alloc();return std::malloc(n);}
template<class... T>void release(void* p,const T&...){std::free(p);}
}
#define HEAP_NEW(symbol,parameters,arguments) \
    extern "C" void* __real_##symbol parameters {return allocate arguments;} \
    extern "C" void* __wrap_##symbol parameters;
#define HEAP_DELETE(symbol,parameters,arguments) \
    extern "C" void __real_##symbol parameters {release arguments;} \
    extern "C" void __wrap_##symbol parameters;
#include "heap_new_wraps.inc"
class HeapNewCallers:public ::testing::Test {
protected:
    void SetUp() override{fail=false;heap_test_isr=false;ASSERT_TRUE(dg::heap_new_init());dg::heap_new_begin();}
    void TearDown() override{dg::heap_new_end();heap_test_isr=false;}
};
TEST_F(HeapNewCallers,ScalarArrayAlignedAndNothrowWrappersPreservePointersAndCaller){
    auto* p=__wrap__Znwj(32);ASSERT_NE(p,nullptr);EXPECT_NE(dg::heap_new_caller(p),0u);__wrap__ZdlPvj(p,32);EXPECT_EQ(dg::heap_new_caller(p),0u);
    p=__wrap__ZnajRKSt9nothrow_t(64,std::nothrow);EXPECT_NE(dg::heap_new_caller(p),0u);__wrap__ZdaPvRKSt9nothrow_t(p,std::nothrow);EXPECT_EQ(dg::heap_new_caller(p),0u);
    p=__wrap__ZnwjSt11align_val_t(128,std::align_val_t(64));EXPECT_NE(dg::heap_new_caller(p),0u);__wrap__ZdlPvjSt11align_val_t(p,128,std::align_val_t(64));EXPECT_EQ(dg::heap_new_caller(p),0u);
}
TEST_F(HeapNewCallers,RealNewExceptionsAreNotSwallowed){fail=true;EXPECT_THROW(__wrap__Znwj(32),std::bad_alloc);EXPECT_FALSE(dg::heap_new_overflow());}
TEST_F(HeapNewCallers,FreezePreservesStopTimeCallersAndNextWindowResets){
    auto* p=reinterpret_cast<void*>(0x2000);dg::heap_new_remember(p,123);dg::heap_new_end();dg::heap_new_forget(p);dg::heap_new_remember(p,456);
    EXPECT_EQ(dg::heap_new_caller(p),123u);dg::heap_new_begin();EXPECT_EQ(dg::heap_new_caller(p),0u);
}
TEST_F(HeapNewCallers,DeletedAddressesCanBeReusedWithoutStaleCaller){
    auto* p=reinterpret_cast<void*>(0x2000);dg::heap_new_remember(p,123);dg::heap_new_forget(p);dg::heap_new_remember(p,456);EXPECT_EQ(dg::heap_new_caller(p),456u);
}
TEST_F(HeapNewCallers,CapacityFailureAndIsrCoverageAreExplicitAndResetPerWindow){
    for(size_t n=0;n<dg::HeapNewCapacity;++n)dg::heap_new_remember(reinterpret_cast<void*>(0x10000+n*16),n+1);
    EXPECT_FALSE(dg::heap_new_overflow());dg::heap_new_remember(reinterpret_cast<void*>(0x10000+dg::HeapNewCapacity*16),9);EXPECT_TRUE(dg::heap_new_overflow());
    dg::heap_new_begin();EXPECT_FALSE(dg::heap_new_overflow());heap_test_isr=true;dg::heap_new_remember(reinterpret_cast<void*>(0x2000),1);EXPECT_TRUE(dg::heap_new_overflow());
}
