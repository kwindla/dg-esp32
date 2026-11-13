#include "optional_measures.h"
#include "owner_totals.h"
#include "compact_timing.h"
#include <gtest/gtest.h>
#include <cstdlib>
#include <stdexcept>

using namespace pipecat::esp_peer;
namespace {
struct Measure {OwnerTotals totals;CompactTiming histogram;};
using Storage=OptionalMeasures<Measure,16,13>;
unsigned allocations=0,releases=0;size_t bytes=0;
void* allocate(size_t n) {++allocations;bytes=n;return std::malloc(n);}
void release(void* p) {++releases;std::free(p);}
struct OptionalMeasureTests:testing::Test {
    void SetUp() override {allocations=releases=0;bytes=0;}
};
}
TEST_F(OptionalMeasureTests, DisabledDetailedCountersAllocateNothingAndKeepHotCounter) {
    Storage storage(false,allocate,release);
    EXPECT_EQ(allocations,0u);
    for(unsigned i=0;i<16;++i)EXPECT_EQ(storage.enabled(i),i==13);
    storage[13].histogram.add(123);storage[13].totals.add(1000,123);
    EXPECT_EQ(storage[13].histogram.count,1u);
    EXPECT_EQ(storage[13].totals.read().first,1000u);
    EXPECT_LE(sizeof(storage),sizeof(Measure)+2*sizeof(void*)+alignof(Measure));
}
TEST_F(OptionalMeasureTests, DetailedCountersAreAllocatedOnceAndNeverAliasHotOrEachOther) {
    {
        Storage storage(true,allocate,release);
        EXPECT_EQ(allocations,1u);EXPECT_EQ(bytes,15*sizeof(Measure));
        for(unsigned i=0;i<16;++i) {
            ASSERT_TRUE(storage.enabled(i));
            EXPECT_EQ(storage[i].histogram.count,0u);
            storage[i].histogram.add(10+i);storage[i].totals.add(i*2,10+i);
        }
        const auto& observed=storage;
        for(unsigned i=0;i<16;++i) {
            EXPECT_EQ(observed[i].histogram.count,1u);
            EXPECT_EQ(observed[i].histogram.maximum,10+i);
            EXPECT_EQ(observed[i].totals.read().first,i*2);
            for(unsigned j=0;j<i;++j)EXPECT_NE(&observed[i],&observed[j]);
        }
        EXPECT_FALSE(storage.enabled(16));EXPECT_EQ(allocations,1u);
    }
    EXPECT_EQ(releases,1u);
}
TEST_F(OptionalMeasureTests, AllocationFailureIsNotSilentlyReportedAsEnabled) {
    EXPECT_THROW((Storage(true,[](size_t)->void* {return nullptr;},release)),std::bad_alloc);
    EXPECT_EQ(releases,0u);
}
TEST_F(OptionalMeasureTests, PartialConstructionReleasesConstructedCountersAndAllocation) {
    struct Throwing {
        static int& alive() {static int n=0;return n;}
        Throwing() {if(alive()==3)throw std::runtime_error("injected");++alive();}
        ~Throwing() {--alive();}
    };
    using Failing=OptionalMeasures<Throwing,5,2>;
    EXPECT_THROW((Failing(true,allocate,release)),std::runtime_error);
    EXPECT_EQ(Throwing::alive(),0);EXPECT_EQ(allocations,1u);EXPECT_EQ(releases,1u);
}
