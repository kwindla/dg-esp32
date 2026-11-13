#include "pthread_config.h"
#include <gtest/gtest.h>
static esp_pthread_cfg_t current;
static bool configured=false,fail_set=false;
esp_pthread_cfg_t esp_pthread_get_default_config() {return {};}
int esp_pthread_get_cfg(esp_pthread_cfg_t* cfg) {if(!configured){*cfg={0,nullptr};return -1;}*cfg=current;return ESP_OK;}
int esp_pthread_set_cfg(const esp_pthread_cfg_t* cfg) {if(fail_set)return -1;current=*cfg;configured=true;return ESP_OK;}
TEST(PthreadConfig, RestoresCallerOnExceptionAndNormalExit) {
    configured=true;current.stack_size=777;
    try {dg::PthreadConfig guard;auto cfg=guard.current();cfg.stack_size=16384;guard.set(cfg);throw 1;}catch(int){}
    EXPECT_EQ(current.stack_size,777u);
    {dg::PthreadConfig guard;auto cfg=guard.current();cfg.stack_size=8192;guard.set(cfg);}
    EXPECT_EQ(current.stack_size,777u);
}
TEST(PthreadConfig, MissingCallerConfigRestoresDefault) {
    configured=false;
    {dg::PthreadConfig guard;auto cfg=guard.current();cfg.stack_size=8192;guard.set(cfg);}
    EXPECT_EQ(current.stack_size,1024u);
}
TEST(PthreadConfig, FailedSetDoesNotLaunchWithWrongConfig) {
    configured=true;current.stack_size=777;
    {dg::PthreadConfig guard;fail_set=true;EXPECT_THROW(guard.set({}),std::runtime_error);fail_set=false;}
    EXPECT_EQ(current.stack_size,777u);
}
