// Compile the real DemoBoot worker; fake only hardware/App/settings boundaries.
#include "demo_boot.h"
#include <gtest/gtest.h>
#include <cstring>
#include <chrono>
#include <thread>
using namespace std::chrono_literals;
namespace dg {
DemoSettings settings;
DemoSettings demo_settings() {
 // Pause the WORKER between its held check and activated check. Main-thread
 // status/configure remains usable. A complete hold/run happens in this gap.
 if(boot_fake::pause && boot_fake::requests>0) {
  boot_fake::paused=true;
  while(boot_fake::pause)std::this_thread::sleep_for(1ms);
 }
 return settings;
}
esp_err_t demo_settings_save(const DemoSettings& s) {
 if(boot_fake::session)return ESP_ERR_INVALID_STATE;
 settings=s;return ESP_OK;
}
}
template<class F> bool eventually(F predicate) {
 const auto end=std::chrono::steady_clock::now()+1500ms;
 while(!predicate() && std::chrono::steady_clock::now()<end)std::this_thread::sleep_for(1ms);
 return predicate();
}
TEST(DemoBootLoop, FastHoldConfigureRunRearmsWithoutPollingHeld) {
 dg::settings.configured=true;dg::settings.enabled=true;
 std::strcpy(dg::settings.url,"http://test");
 boot_fake::requests=0;boot_fake::ended=0;boot_fake::session=false;boot_fake::idle=true;
 dg::App app;dg_audio::Pipeline audio;std::atomic<bool> capture{false};
 dg::DemoBoot demo(app,audio,capture);
 const bool connected=eventually([]{return boot_fake::requests==1;});
 EXPECT_TRUE(connected);
 if(!connected)return;
 boot_fake::pause=true;
 const bool paused=eventually([]{return boot_fake::paused.load();});
 EXPECT_TRUE(paused);
 if(!paused){boot_fake::pause=false;return;}
 // The actual loop cannot see held=true: this is deterministic, independent
 // of host scheduling or a nominal 50 ms polling interval.
 EXPECT_NO_THROW(demo.hold());
 EXPECT_TRUE(boot_fake::idle);EXPECT_FALSE(boot_fake::session);
 EXPECT_EQ(boot_fake::ended,1u);EXPECT_FALSE(capture);
 EXPECT_NO_THROW(demo.configure(dg::settings));
 demo.resume();boot_fake::pause=false;
 EXPECT_TRUE(eventually([]{return boot_fake::requests==2;}));
 EXPECT_TRUE(capture);
}
