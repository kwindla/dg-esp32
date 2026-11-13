#include "../backends/jd9365_dsi/framebuffer.h"
#include <gtest/gtest.h>
#include <algorithm>
#include <vector>
#include <cstring>
#include <functional>
using namespace dg_display;
namespace {
std::vector<uint16_t> wire(std::initializer_list<uint16_t> values){
    std::vector<uint16_t> data(values.size());auto* bytes=reinterpret_cast<uint8_t*>(data.data());size_t i=0;
    for(auto value:values){bytes[i++]=uint8_t(value>>8);bytes[i++]=uint8_t(value);}return data;
}
struct Rig {
    DsiFramebuffer panel;std::vector<uint16_t> fb;
    struct Sync {void* start;size_t bytes;};std::vector<Sync> syncs;
    std::vector<DsiFramebuffer::Event> events;std::vector<ScanOutcome> callbacks;
    uint32_t now=100;bool sync_ok=true;unsigned released=0;
    std::function<void()> during_sync;
    explicit Rig(size_t stride=800,uint32_t frame_seed=0):panel(frame_seed),fb(stride*800+2,0xaaaa){
        DsiFramebuffer::Hooks hooks;hooks.context=this;
        hooks.msync=[](void* p,void* start,size_t bytes){auto& r=*static_cast<Rig*>(p);r.syncs.push_back({start,bytes});if(r.during_sync)r.during_sync();return r.sync_ok;};
        hooks.clock=[](void* p){return static_cast<Rig*>(p)->now;};
        hooks.settled=[](void* p,const DsiFramebuffer::Event& e){static_cast<Rig*>(p)->events.push_back(e);};
        EXPECT_EQ(panel.attach(fb.data()+1,{800,800,false,PixelFormat::Rgb565BE,stride,false},hooks),PresentError::None);
    }
    Released release(){return {[](void* p){++static_cast<Rig*>(p)->released;},this};}
    ScanCallback callback(){return {[](void* p,SubmitId,ScanOutcome o){static_cast<Rig*>(p)->callbacks.push_back(o);},this};}
    void frame(uint32_t us){panel.frame_complete(us);}
    uint16_t pixel(size_t x,size_t y,size_t stride=800)const{
        const auto* b=reinterpret_cast<const uint8_t*>(fb.data()+1+y*stride+x);return uint16_t(b[0]|uint16_t(b[1])<<8);
    }
};
}
TEST(DsiFramebuffer,ConvertsPrimaryColorsFromBEIntoLEAndReleasesAfterMsync){
    Rig r;auto px=wire({0xf800,0x07e0,0x001f,0xffff,0});
    auto id=r.panel.submit({0,0,5,1},px.data(),5,r.release());ASSERT_TRUE(id);
    EXPECT_EQ(r.released,1u);ASSERT_EQ(r.syncs.size(),1u);
    EXPECT_EQ(r.panel.wait_released(id),PresentError::None);EXPECT_EQ(r.panel.committed(id),PresentError::None);
    const uint8_t expected[]={0x00,0xf8,0xe0,0x07,0x1f,0x00,0xff,0xff,0x00,0x00};
    EXPECT_EQ(std::memcmp(r.fb.data()+1,expected,sizeof expected),0);
    EXPECT_TRUE(r.panel.info().scan_fence);EXPECT_EQ(r.panel.info().format,PixelFormat::Rgb565BE);
    EXPECT_EQ(r.fb.front(),0xaaaa);EXPECT_EQ(r.fb.back(),0xaaaa);
}
TEST(DsiFramebuffer,SourceStrideAndFramebufferPitchAreIndependentAndSyncCoversWholeRows){
    Rig r(808);auto px=wire({0xf800,0x07e0,0x1111,0x001f,0xffff,0x2222});
    auto id=r.panel.submit({798,798,800,800},px.data(),3);ASSERT_TRUE(id);
    EXPECT_EQ(r.pixel(798,798,808),0xf800);EXPECT_EQ(r.pixel(799,798,808),0x07e0);
    EXPECT_EQ(r.pixel(798,799,808),0x001f);EXPECT_EQ(r.pixel(799,799,808),0xffff);
    EXPECT_EQ(r.fb[1+798*808+797],0xaaaa);EXPECT_EQ(r.fb[1+798*808+800],0xaaaa);
    ASSERT_EQ(r.syncs.size(),1u);EXPECT_EQ(r.syncs[0].start,r.fb.data()+1+798*808);
    EXPECT_EQ(r.syncs[0].bytes,2u*808*2);EXPECT_EQ(r.fb.front(),0xaaaa);EXPECT_EQ(r.fb.back(),0xaaaa);
}
TEST(DsiFramebuffer,ExactBottomRightPixelIsAcceptedAndOverrunsNeverCopySyncOrRelease){
    Rig r;auto px=wire({0x1234,0x5678});
    ASSERT_TRUE(r.panel.submit({799,799,800,800},px.data(),1));EXPECT_EQ(r.pixel(799,799),0x1234);
    const auto before=r.fb;
    for(auto rect:{Rect{799,799,801,800},Rect{799,799,800,801},Rect{-1,0,1,1},Rect{0,-1,1,1},Rect{0,0,0,1}})
        EXPECT_EQ(r.panel.submit(rect,px.data(),2,r.release()).error,PresentError::Invalid);
    EXPECT_EQ(r.panel.submit({0,0,2,1},px.data(),1,r.release()).error,PresentError::Invalid);
    EXPECT_EQ(r.panel.submit({0,0,1,1},nullptr,1,r.release()).error,PresentError::Invalid);
    EXPECT_EQ(r.fb,before);EXPECT_EQ(r.syncs.size(),1u);EXPECT_EQ(r.released,0u);
}
TEST(DsiFramebuffer,NoCommitOrSourceReleaseIsPublishedBeforeSyncAndSyncFailureFailsClosed){
    Rig r;auto px=wire({0xf800});
    r.during_sync=[&]{EXPECT_EQ(r.panel.committed({1}),PresentError::Invalid);EXPECT_EQ(r.released,0u);EXPECT_TRUE(r.events.empty());};
    r.sync_ok=false;EXPECT_EQ(r.panel.submit({0,0,1,1},px.data(),1,r.release()).error,PresentError::Io);
    EXPECT_EQ(r.released,0u);EXPECT_TRUE(r.events.empty());EXPECT_EQ(r.panel.committed({1}),PresentError::Io);
    r.sync_ok=true;EXPECT_EQ(r.panel.submit({0,0,1,1},px.data(),1).error,PresentError::Io);
    EXPECT_EQ(r.syncs.size(),1u);
}
TEST(DsiFramebuffer,FullScanFinishingDuringNewMsyncWinsBeforeOverlapSupersession){
    Rig r;auto px=wire({1});auto a=r.panel.submit({0,0,1,1},px.data(),1);ASSERT_TRUE(a);
    r.panel.scan_fence(a,r.callback());r.frame(18000);r.now=37000;
    r.during_sync=[&]{EXPECT_TRUE(r.callbacks.empty());r.frame(36000);};
    ASSERT_TRUE(r.panel.submit({0,0,1,1},px.data(),1));
    ASSERT_EQ(r.callbacks.size(),1u);EXPECT_EQ(r.callbacks[0],ScanOutcome::Presented);
}
TEST(DsiFramebuffer,PresentedNeedsTwoCompletionsAfterCommitAndCallbacksRunOnlyOnOwner){
    Rig r;auto px=wire({0xf800});auto id=r.panel.submit({0,0,1,1},px.data(),1,r.release());ASSERT_TRUE(id);
    EXPECT_EQ(r.panel.scan_fence(id,r.callback()),PresentError::None);
    r.frame(18000);r.panel.poll();EXPECT_TRUE(r.callbacks.empty());
    r.frame(36000);EXPECT_TRUE(r.callbacks.empty());r.panel.poll();
    ASSERT_EQ(r.callbacks.size(),1u);EXPECT_EQ(r.callbacks[0],ScanOutcome::Presented);
    ASSERT_EQ(r.events.size(),1u);EXPECT_EQ(r.events[0].committed_us,100u);EXPECT_EQ(r.events[0].completed_us,36000u);EXPECT_TRUE(r.events[0].timed);
    r.frame(54000);r.panel.poll();EXPECT_EQ(r.callbacks.size(),1u);
    EXPECT_EQ(r.panel.scan_fence(id,r.callback()),PresentError::Invalid);
}
TEST(DsiFramebuffer,FrameAlreadyInProgressAtCommitDoesNotCountAsFullScan){
    Rig r;auto px=wire({1});r.frame(18000);r.now=18100;
    auto id=r.panel.submit({0,0,1,1},px.data(),1);ASSERT_TRUE(id);r.panel.scan_fence(id,r.callback());
    r.frame(36000);r.panel.poll();EXPECT_TRUE(r.callbacks.empty());
    r.frame(54000);r.panel.poll();ASSERT_EQ(r.callbacks.size(),1u);EXPECT_EQ(r.callbacks[0],ScanOutcome::Presented);
}
TEST(DsiFramebuffer,OverlappingLaterCommitSupersedesButOnlyAfterItsMsync){
    Rig r;auto px=wire({1,2,3,4});auto a=r.panel.submit({0,0,2,2},px.data(),2);ASSERT_TRUE(a);
    r.panel.scan_fence(a,r.callback());r.frame(18000);r.now=19000;
    auto b=r.panel.submit({1,1,3,3},px.data(),2);ASSERT_TRUE(b);
    ASSERT_EQ(r.callbacks.size(),1u);EXPECT_EQ(r.callbacks[0],ScanOutcome::Superseded);
    ASSERT_EQ(r.syncs.size(),2u);EXPECT_EQ(r.panel.committed(a),PresentError::None);
    EXPECT_EQ(r.panel.scan_fence(b,r.callback()),PresentError::None);
    r.frame(36000);r.panel.poll();EXPECT_EQ(r.callbacks.size(),1u);
    r.frame(54000);r.panel.poll();ASSERT_EQ(r.callbacks.size(),2u);EXPECT_EQ(r.callbacks[1],ScanOutcome::Presented);
}
TEST(DsiFramebuffer,AdjacentSubmissionsDoNotSupersedeAndLateRegisteredFenceKeepsOutcome){
    Rig r;auto px=wire({1});auto a=r.panel.submit({0,0,1,1},px.data(),1);ASSERT_TRUE(a);
    auto b=r.panel.submit({1,0,2,1},px.data(),1);ASSERT_TRUE(b);
    r.frame(18000);r.frame(36000);r.panel.poll();
    EXPECT_EQ(r.panel.scan_fence(a,r.callback()),PresentError::None);EXPECT_EQ(r.panel.scan_fence(b,r.callback()),PresentError::None);
    ASSERT_EQ(r.callbacks.size(),2u);EXPECT_EQ(r.callbacks[0],ScanOutcome::Presented);EXPECT_EQ(r.callbacks[1],ScanOutcome::Presented);
}
TEST(DsiFramebuffer,CompletedFullScanWinsEvenWhenOwnerPollWasDelayedPastOverlappingCommit){
    Rig r;auto px=wire({1});auto a=r.panel.submit({0,0,1,1},px.data(),1);ASSERT_TRUE(a);
    r.frame(18000);r.frame(36000);r.now=37000;
    ASSERT_TRUE(r.panel.submit({0,0,1,1},px.data(),1));
    r.panel.scan_fence(a,r.callback());ASSERT_EQ(r.callbacks.size(),1u);EXPECT_EQ(r.callbacks[0],ScanOutcome::Presented);
}
TEST(DsiFramebuffer,ExpiredHistoryIsUnavailableAndPendingFencesAreNeverEvicted){
    Rig r;auto px=wire({1});SubmitId first;
    for(size_t i=0;i<DsiFramebuffer::History;++i){auto id=r.panel.submit({int(i),0,int(i)+1,1},px.data(),1);ASSERT_TRUE(id);if(!i)first=id;}
    EXPECT_EQ(r.panel.submit({400,0,401,1},px.data(),1,r.release()).error,PresentError::Busy);EXPECT_EQ(r.released,0u);
    EXPECT_EQ(r.panel.scan_fence(first,r.callback()),PresentError::None);
    r.frame(18000);r.frame(36000);r.panel.poll();ASSERT_EQ(r.callbacks.size(),1u);
    ASSERT_TRUE(r.panel.submit({400,0,401,1},px.data(),1));
    EXPECT_EQ(r.panel.committed(first),PresentError::Unavailable);EXPECT_EQ(r.panel.scan_fence(first,r.callback()),PresentError::Unavailable);
    EXPECT_EQ(r.panel.committed({10000}),PresentError::Invalid);
}
TEST(DsiFramebuffer,OverwrittenTimestampNeverInventsLatencyAndMicrosWrapIsUnsigned){
    Rig r;auto px=wire({1});r.now=0xfffffff0u;
    auto a=r.panel.submit({0,0,1,1},px.data(),1);ASSERT_TRUE(a);
    r.frame(1000);r.frame(2000);r.panel.poll();ASSERT_EQ(r.events.size(),1u);
    EXPECT_EQ(uint32_t(r.events[0].completed_us-r.events[0].committed_us),2016u);
    auto b=r.panel.submit({2,0,3,1},px.data(),1);ASSERT_TRUE(b);
    for(unsigned i=0;i<40;++i)r.frame(3000+i*18000);
    r.panel.poll();ASSERT_EQ(r.events.size(),2u);EXPECT_FALSE(r.events[1].timed);EXPECT_EQ(r.events[1].outcome,ScanOutcome::Presented);
}
TEST(DsiFramebuffer,P4CanvasHasFortyPixelBordersAndScaleTwoReplicationAtBothCorners){
    // Explicit native-test layout: all linked display_core TUs retain the same
    // host canvas definitions. canvas_P4XC separately tests the board constants.
    Rig r;std::vector<uint16_t> scratch(40960);
    LogicalAdapter adapter(r.panel,{360,360,2,40,40,0},scratch.data(),scratch.size());
    ASSERT_EQ(adapter.fill_border(),PresentError::None);
    for(int y=0;y<800;++y)for(int x=0;x<800;++x)
        if(x<40 || y<40 || x>=760 || y>=760){ASSERT_EQ(r.pixel(x,y),0)<<x<<","<<y;}
    EXPECT_EQ(r.fb[1+40*800+40],0xaaaa);EXPECT_EQ(r.fb[1+759*800+759],0xaaaa);
    auto px=wire({0xf800});ASSERT_TRUE(adapter.submit({0,0,1,1},px.data(),1));
    ASSERT_TRUE(adapter.submit({359,359,360,360},px.data(),1));
    for(int y:{40,41,758,759})for(int x:{40,41,758,759})
        if((x<42)==(y<42)){EXPECT_EQ(r.pixel(x,y),0xf800);}
    EXPECT_EQ(r.pixel(39,40),0);EXPECT_EQ(r.pixel(760,759),0);
}
TEST(DsiFramebuffer,StackedFullWidthStripesAreAllPresented){
    Rig r;std::vector<uint16_t> px(800*24,0);
    for(int y:{0,24,48}){
        auto id=r.panel.submit({0,y,800,y+24},px.data(),800);ASSERT_TRUE(id);
        ASSERT_EQ(r.panel.scan_fence(id,r.callback()),PresentError::None);
    }
    EXPECT_TRUE(r.events.empty());r.frame(18000);r.panel.poll();EXPECT_TRUE(r.callbacks.empty());
    r.frame(36000);r.panel.poll();ASSERT_EQ(r.callbacks.size(),3u);
    for(auto outcome:r.callbacks)EXPECT_EQ(outcome,ScanOutcome::Presented);
}
TEST(DsiFramebuffer,HorizontalLineSupersedesOnlyTheMiddleStackedStripe){
    Rig r;std::vector<uint16_t> px(800*24,0);std::vector<SubmitId> ids;
    for(int y:{0,24,48}){
        auto id=r.panel.submit({0,y,800,y+24},px.data(),800);ASSERT_TRUE(id);ids.push_back(id);
        ASSERT_EQ(r.panel.scan_fence(id,r.callback()),PresentError::None);
    }
    auto line=r.panel.submit({0,30,800,31},px.data(),800);ASSERT_TRUE(line);
    ASSERT_EQ(r.events.size(),1u);EXPECT_EQ(r.events[0].id.value,ids[1].value);
    EXPECT_EQ(r.events[0].outcome,ScanOutcome::Superseded);
    ASSERT_EQ(r.panel.scan_fence(line,r.callback()),PresentError::None);
    r.frame(18000);r.frame(36000);r.panel.poll();ASSERT_EQ(r.events.size(),4u);
    for(auto id:{ids[0],ids[2],line}){
        auto found=std::find_if(r.events.begin(),r.events.end(),[&](const auto& event){return event.id.value==id.value;});
        ASSERT_NE(found,r.events.end());EXPECT_EQ(found->outcome,ScanOutcome::Presented);
    }
}
TEST(DsiFramebuffer,VerticallyAdjacentPixelsDoNotSupersede){
    Rig r;auto px=wire({1});
    for(int y:{0,1}){
        auto id=r.panel.submit({0,y,1,y+1},px.data(),1);ASSERT_TRUE(id);
        ASSERT_EQ(r.panel.scan_fence(id,r.callback()),PresentError::None);
    }
    EXPECT_TRUE(r.events.empty());r.frame(18000);r.frame(36000);r.panel.poll();
    ASSERT_EQ(r.callbacks.size(),2u);for(auto outcome:r.callbacks)EXPECT_EQ(outcome,ScanOutcome::Presented);
}
TEST(DsiFramebuffer,FrameCounterWrapKeepsTwoCompletionFenceAndTimestamp){
    for(uint32_t seed:{UINT32_MAX-1,UINT32_MAX}){
        Rig r(800,seed);auto px=wire({1});
        auto id=r.panel.submit({0,0,1,1},px.data(),1);ASSERT_TRUE(id);
        ASSERT_EQ(r.panel.scan_fence(id,r.callback()),PresentError::None);EXPECT_TRUE(r.callbacks.empty());
        r.frame(18000);r.panel.poll();EXPECT_TRUE(r.callbacks.empty());
        r.frame(36000);r.panel.poll();ASSERT_EQ(r.callbacks.size(),1u);
        EXPECT_EQ(r.callbacks[0],ScanOutcome::Presented);ASSERT_EQ(r.events.size(),1u);
        EXPECT_TRUE(r.events[0].timed);EXPECT_EQ(r.events[0].completed_us,36000u);
    }
}
TEST(DsiFramebuffer,NativeOwnerReturnRestoresBordersAndRedrawsLatestLogicalScene){
    Rig r;std::vector<uint16_t> scratch(40960);LogicalAdapter adapter(r.panel,{360,360,2,40,40,0},scratch.data(),scratch.size());
    struct Context {Presenter* presenter=nullptr;std::vector<uint16_t> px=wire({0x001f});bool called=false;} context;
    Presenter presenter(adapter,[](void* p){auto& c=*static_cast<Context*>(p);c.called=true;
        return c.presenter->submit_logical({0,0,1,1},c.px.data(),1).error;},&context);context.presenter=&presenter;
    ASSERT_EQ(presenter.set_mode(PresentMode::Native),PresentError::None);auto px=wire({0xf800});
    ASSERT_TRUE(presenter.submit_native({0,0,1,1},px.data(),1));EXPECT_EQ(presenter.submit_logical({0,0,1,1},px.data(),1).error,PresentError::Inactive);
    ASSERT_EQ(presenter.set_mode(PresentMode::Logical),PresentError::None);EXPECT_TRUE(context.called);
    EXPECT_EQ(r.pixel(0,0),0);EXPECT_EQ(r.pixel(40,40),0x001f);EXPECT_EQ(r.pixel(41,41),0x001f);
    EXPECT_EQ(presenter.set_mode(PresentMode::Native),PresentError::None);
}
