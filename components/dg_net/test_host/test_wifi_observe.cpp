#include <gtest/gtest.h>
#include <cstdlib>
#include <climits>
#include "../wifi_observe.cpp" // Compile/exercise the production body in both configurations.
#if CONFIG_DG_WIFI_OBSERVE
namespace {
int allocations=0,frees=0,ps_reads=0,ap_reads=0,registrations=0,task_creates=0;
int ps_result=ESP_OK,ap_result=ESP_OK,register_result=ESP_OK;
bool fail_allocate=false,fail_task=false,internal_storage=true;
wifi_tx_done_cb_t callback=nullptr;
int64_t fake_time=1000;
wifi_ps_type_t fake_ps=WIFI_PS_MAX_MODEM;
struct Delay {unsigned ticks;};
class Observe : public testing::Test {
 void SetUp() override {
    if(history)std::free(history);
    history=nullptr;status={};sampler=nullptr;previous_ok=previous_fail=0;previous_end_us=0;
    completed_ok=0;completed_fail=0;allocations=frees=ps_reads=ap_reads=registrations=task_creates=0;
    ps_result=ap_result=register_result=ESP_OK;fail_allocate=fail_task=false;internal_storage=true;callback=nullptr;fake_time=1000;
 }
};
wifi_init_config_t config{16,32,10,32,0,1,1,16,0};
}
int64_t esp_timer_get_time(){return fake_time+=10;}
bool esp_ptr_internal(const void*){return internal_storage;}
void* heap_caps_calloc(size_t n,size_t size,unsigned caps) {
 ++allocations;EXPECT_EQ(caps,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);return fail_allocate?nullptr:std::calloc(n,size);
}
void heap_caps_free(void* p){++frees;std::free(p);}
int esp_wifi_get_ps(wifi_ps_type_t* p){++ps_reads;*p=fake_ps;return ps_result;}
int esp_wifi_sta_get_ap_info(wifi_ap_record_t* p){++ap_reads;*p={-37,6,0,{1,2,3,4,5,6}};return ap_result;}
int esp_wifi_set_tx_done_cb(wifi_tx_done_cb_t cb){++registrations;if(register_result==ESP_OK)callback=cb;return register_result;}
int xTaskCreatePinnedToCoreWithCaps(void(*f)(void*),const char* name,unsigned bytes,void*,unsigned priority,TaskHandle_t* out,int core,unsigned caps) {
 ++task_creates;EXPECT_EQ(f,run);EXPECT_STREQ(name,"wifi-observe");EXPECT_EQ(bytes,4096u);
 EXPECT_EQ(priority,1u);EXPECT_EQ(core,0);EXPECT_EQ(caps,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 *out=fail_task?nullptr:reinterpret_cast<void*>(1);return fail_task?0:pdPASS;
}
void vTaskDelay(unsigned ticks){throw Delay{ticks};}

TEST_F(Observe,RegistersOneObserverWithoutChangingNetworkPolicy) {
 ASSERT_EQ(dg_wifi_observe_start(&config),ESP_OK);ASSERT_NE(callback,nullptr);
 auto s=dg_wifi_observe_status();EXPECT_TRUE(s.enabled);EXPECT_TRUE(s.tx_done_registered);
 EXPECT_EQ(s.static_tx_buffers,16);EXPECT_EQ(s.cache_tx_buffers,32);EXPECT_EQ(s.rx_ba_window,16);
 EXPECT_EQ(ps_reads,0);EXPECT_EQ(ap_reads,0);EXPECT_EQ(allocations,1);EXPECT_EQ(registrations,1);
 EXPECT_EQ(dg_wifi_observe_start(&config),ESP_ERR_INVALID_STATE);EXPECT_EQ(registrations,1);
}
TEST_F(Observe,CallbackOnlyCountsStaCompletionAndWrapsUnsignedDeltas) {
 ASSERT_EQ(dg_wifi_observe_start(&config),ESP_OK);
 callback(WIFI_IF_AP,nullptr,nullptr,false);
 callback(WIFI_IF_STA,nullptr,nullptr,false);callback(WIFI_IF_STA,nullptr,nullptr,true);
 EXPECT_EQ(allocations,1);EXPECT_EQ(ps_reads,0);EXPECT_EQ(ap_reads,0);EXPECT_EQ(registrations,1);
 sample_once();dg_wifi_observation_t r{};ASSERT_TRUE(dg_wifi_observe_read(1,&r));
 EXPECT_EQ(r.tx_done_success,1u);EXPECT_EQ(r.tx_done_failed,1u);
 previous_ok=UINT_MAX-2;completed_ok=1;sample_once();ASSERT_TRUE(dg_wifi_observe_read(2,&r));
 EXPECT_EQ(r.tx_done_success,4u);EXPECT_EQ(r.tx_done_failed,0u);
}
TEST_F(Observe,ReadbackErrorsStayExplicitAndCopiesMakeNoDriverCalls) {
 ASSERT_EQ(dg_wifi_observe_start(&config),ESP_OK);sample_once();
 dg_wifi_observation_t r{};ASSERT_TRUE(dg_wifi_observe_read(1,&r));
 EXPECT_EQ(r.ps_mode,WIFI_PS_MAX_MODEM);EXPECT_EQ(r.rssi_dbm,-37);EXPECT_EQ(r.primary_channel,6);
 EXPECT_LE(r.counter_begin_us,r.begin_us);EXPECT_LT(r.begin_us,r.end_us);
 ps_result=ESP_ERR_INVALID_STATE;ap_result=ESP_ERR_INVALID_STATE;sample_once();
 ASSERT_TRUE(dg_wifi_observe_read(2,&r));EXPECT_EQ(r.ps_error,ESP_ERR_INVALID_STATE);EXPECT_EQ(r.ap_error,ESP_ERR_INVALID_STATE);
 EXPECT_EQ(ps_reads,2);EXPECT_EQ(ap_reads,2);dg_wifi_observe_status();dg_wifi_observe_read(2,&r);
 EXPECT_EQ(ps_reads,2);EXPECT_EQ(ap_reads,2);EXPECT_FALSE(dg_wifi_observe_read(0,&r));EXPECT_FALSE(dg_wifi_observe_read(1,nullptr));
}
TEST_F(Observe,RingOverwritesAreCountedAndOldSequenceCannotBeFabricated) {
 ASSERT_EQ(dg_wifi_observe_start(&config),ESP_OK);
 for(unsigned i=0;i<DG_WIFI_OBSERVE_CAPACITY+7;++i)sample_once();
 auto s=dg_wifi_observe_status();EXPECT_EQ(s.overwritten,7u);EXPECT_EQ(s.retained,256u);
 EXPECT_EQ(s.first_sequence,8u);dg_wifi_observation_t r{};
 EXPECT_FALSE(dg_wifi_observe_read(7,&r));ASSERT_TRUE(dg_wifi_observe_read(8,&r));EXPECT_EQ(r.sequence,8u);
 EXPECT_FALSE(dg_wifi_observe_read(s.last_sequence+1,&r));
}
TEST_F(Observe,AllocationOrRegistrationFailureDoesNotStartTask) {
 fail_allocate=true;EXPECT_EQ(dg_wifi_observe_start(&config),ESP_ERR_NO_MEM);
 EXPECT_EQ(registrations,0);EXPECT_EQ(task_creates,0);
 status={};fail_allocate=false;register_result=ESP_ERR_INVALID_STATE;
 EXPECT_EQ(dg_wifi_observe_start(&config),ESP_ERR_INVALID_STATE);EXPECT_EQ(task_creates,0);EXPECT_EQ(frees,1);
 EXPECT_FALSE(dg_wifi_observe_status().tx_done_registered);
}
TEST_F(Observe,RejectsNoninternalCounterStorageBeforeAnyAllocationOrCallback) {
 internal_storage=false;EXPECT_EQ(dg_wifi_observe_start(&config),ESP_ERR_INVALID_STATE);
 EXPECT_EQ(allocations,0);EXPECT_EQ(registrations,0);EXPECT_EQ(task_creates,0);
 EXPECT_FALSE(dg_wifi_observe_status().tx_done_registered);
}
TEST_F(Observe,TaskFailureUnregistersOwnedCallbackAndRetainsError) {
 fail_task=true;EXPECT_EQ(dg_wifi_observe_start(&config),ESP_ERR_NO_MEM);
 EXPECT_EQ(callback,nullptr);EXPECT_EQ(registrations,2);EXPECT_EQ(frees,1);
 EXPECT_FALSE(dg_wifi_observe_status().tx_done_registered);EXPECT_EQ(dg_wifi_observe_status().startup_error,ESP_ERR_NO_MEM);
}
TEST_F(Observe,SamplingHasOneSecondDelayAndNoCatchupLoop) {
 ASSERT_EQ(dg_wifi_observe_start(&config),ESP_OK);
 try{run(nullptr);FAIL();}catch(Delay delay){EXPECT_EQ(delay.ticks,1000u);}
 EXPECT_EQ(ps_reads,1);EXPECT_EQ(ap_reads,1);EXPECT_EQ(dg_wifi_observe_status().samples,1u);
}
#else
TEST(ObserveDisabled,NoTaskBufferDriverCallbackOrWifiReadbackDependency) {
 EXPECT_EQ(dg_wifi_observe_start(nullptr),ESP_ERR_NOT_SUPPORTED);
 auto s=dg_wifi_observe_status();EXPECT_FALSE(s.enabled);EXPECT_EQ(s.retained,0u);
 dg_wifi_observation_t r{};EXPECT_FALSE(dg_wifi_observe_read(1,&r));
 // No fake definitions for any task/heap/driver symbol: disabled links only
 // if the actual production body has zero such dependencies.
}
#endif
