#include "../../../main/cache_metrics.h"
#include <gtest/gtest.h>
#include <cstdlib>
namespace pipecat::esp_peer {
void* stat_allocate(size_t n){return std::malloc(n);}
void stat_free(void* p){std::free(p);}
}
TEST(P4Cache,EnabledCountersRemainExplicitJsonNullWithoutRegisterAccess){
    cache_metrics_enable(true);
    EXPECT_TRUE(cache_metrics_snapshot().is_null());
    cache_metrics_enable(false);
    EXPECT_TRUE(cache_metrics_snapshot().is_null());
}
