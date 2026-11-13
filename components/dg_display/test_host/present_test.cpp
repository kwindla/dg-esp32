#include "dg_display/present.h"
#include "dg_display/null_panel.h"
#include "dg_display/engine.h"
#include <gtest/gtest.h>
#include <algorithm>
#include <array>
#include <vector>
using namespace dg_display;
namespace {
struct FakePanel final:PanelBackend {
    struct Transfer {SubmitId id;Rect rect;std::vector<uint16_t> copy;const uint16_t* source=nullptr;size_t stride=0;Released released;ScanCallback fence;bool release=false,commit=false,scan=false,superseded=false;};
    NativeInfo geometry;std::vector<uint16_t> pixels;std::vector<Transfer> transfers;
    const uint16_t* source=nullptr;PresentError fault=PresentError::None,commit_fault=PresentError::None;bool latest_only=false;unsigned commit_calls=0;
    std::vector<unsigned> events;
    FakePanel(int w,int h,bool scan=false):geometry{w,h,false,PixelFormat::Rgb565BE,size_t(w),scan},pixels(size_t(w*h),0x1234){}
    PresentError init()override{return PresentError::None;}
    NativeInfo info()const noexcept override{return geometry;}
    SubmitId submit(Rect r,const uint16_t* px,size_t stride,Released cb={})override{
        if(fault!=PresentError::None)return {0,fault};
        if(!valid_native(r,px,stride,geometry))return {0,PresentError::Invalid};
        source=px;Transfer t;t.id={transfers.size()+1};t.rect=r;t.released=cb;
        t.source=px;t.stride=stride; // DMA keeps reading the source until release, not submit.
        transfers.push_back(t);events.push_back(1);return t.id;
    }
    Transfer* find(SubmitId id){return id && id.value<=transfers.size()?&transfers[size_t(id.value-1)]:nullptr;}
    PresentError wait_released(SubmitId id)override{
        auto* t=find(id);if(!t)return PresentError::Invalid;
        if(!t->release){
            for(auto y=t->rect.y0;y<t->rect.y1;++y)for(auto x=t->rect.x0;x<t->rect.x1;++x)t->copy.push_back(t->source[size_t(y-t->rect.y0)*t->stride+size_t(x-t->rect.x0)]);
            t->release=true;events.push_back(2);t->released();}return PresentError::None;
    }
    static bool overlaps(Rect a,Rect b){return a.x0<b.x1 && b.x0<a.x1 && a.y0<b.y1 && b.y0<a.y1;}
    PresentError committed(SubmitId id)override{
        ++commit_calls;
        if(commit_fault!=PresentError::None)return commit_fault;
        if(latest_only && id.value!=transfers.size())return PresentError::Unavailable;
        auto* t=find(id);if(!t)return PresentError::Invalid;
        if(t->commit)return PresentError::None;
        wait_released(id);t->commit=true;events.push_back(3);
        size_t at=0;for(auto y=t->rect.y0;y<t->rect.y1;++y)for(auto x=t->rect.x0;x<t->rect.x1;++x)pixels[size_t(y)*geometry.w+size_t(x)]=t->copy[at++];
        for(auto& old:transfers)if(old.id.value<id.value && old.commit && !old.scan && overlaps(old.rect,t->rect))old.superseded=true;
        return PresentError::None;
    }
    PresentError scan_fence(SubmitId id,ScanCallback cb)override{
        if(!geometry.scan_fence)return PresentError::Unsupported;
        auto* t=find(id);if(!t || !cb.fn)return PresentError::Invalid;
        if(t->scan || t->fence.fn)return PresentError::Busy;
        t->fence=cb;return PresentError::None;
    }
    void scan(){for(auto& t:transfers)if(t.commit && !t.scan){t.scan=true;if(t.fence.fn){events.push_back(4);t.fence.fn(t.fence.context,t.id,t.superseded?ScanOutcome::Superseded:ScanOutcome::Presented);}}}
    PresentError set_backlight(unsigned pct)override{return pct<=100?PresentError::None:PresentError::Invalid;}
};
Released counter(unsigned& value){return {[](void* p){++*static_cast<unsigned*>(p);},&value};}
}
TEST(Present,ScaleOneIdentityPreservesPointerStrideAndReleaseFence){
    FakePanel panel(6,4);LogicalAdapter adapter(panel,{6,4,1,0,0,0});Presenter presenter(adapter);
    const uint16_t data[]={1,2,99,3,4,99};unsigned released=0;
    auto id=presenter.submit_logical({1,1,3,3},data,3,counter(released));ASSERT_TRUE(id);
    EXPECT_EQ(panel.source,data);EXPECT_EQ(released,0u);EXPECT_EQ(panel.pixels[7],0x1234);
    EXPECT_EQ(presenter.wait_released(id),PresentError::None);EXPECT_EQ(released,1u);
    EXPECT_FALSE(panel.transfers[0].commit);
    EXPECT_EQ(presenter.committed(id),PresentError::None);EXPECT_EQ(released,1u);
    EXPECT_EQ(panel.pixels[7],1);EXPECT_EQ(panel.pixels[8],2);EXPECT_EQ(panel.pixels[13],3);EXPECT_EQ(panel.pixels[14],4);
}
TEST(Present,ScaleTwoReplicatesEveryPixelWithPaddedInputStride){
    FakePanel panel(8,8);std::array<uint16_t,16> scratch{};LogicalAdapter adapter(panel,{4,4,2,0,0,0},scratch.data(),scratch.size());
    const uint16_t data[]={0xf800,0x07e0,99,0x001f,0xffff,99};unsigned released=0;
    auto id=adapter.submit({1,1,3,3},data,3,counter(released));ASSERT_TRUE(id);EXPECT_EQ(released,1u);
    EXPECT_EQ(panel.committed(id),PresentError::None);
    const uint16_t expected[4][4]={{0xf800,0xf800,0x07e0,0x07e0},{0xf800,0xf800,0x07e0,0x07e0},{0x001f,0x001f,0xffff,0xffff},{0x001f,0x001f,0xffff,0xffff}};
    for(unsigned y=0;y<4;++y)for(unsigned x=0;x<4;++x)EXPECT_EQ(panel.pixels[(y+2)*8+x+2],expected[y][x]);
    EXPECT_EQ(panel.pixels[0],0x1234);
}
TEST(Present,EightHundredPanelCenters720SquareAndFillsOnlyFortyPixelBorders){
    FakePanel panel(800,800);std::array<uint16_t,1600> scratch{};LogicalAdapter adapter(panel,{360,360,2,40,40,0xbeef},scratch.data(),scratch.size());
    ASSERT_EQ(adapter.fill_border(),PresentError::None);
    for(unsigned y=0;y<800;++y)for(unsigned x=0;x<800;++x)ASSERT_EQ(panel.pixels[y*800+x],(x<40 || y<40 || x>=760 || y>=760)?0xbeef:0x1234);
    const uint16_t data[]={0x1111,0x2222};auto id=adapter.submit({358,359,360,360},data,2);ASSERT_TRUE(id);ASSERT_EQ(panel.committed(id),PresentError::None);
    EXPECT_EQ(panel.pixels[758*800+756],0x1111);EXPECT_EQ(panel.pixels[759*800+759],0x2222);EXPECT_EQ(panel.pixels[759*800+760],0xbeef);
}
TEST(Present,NativeOwnerSubmitsWithoutScalingAndLogicalSceneReappearsOnSwitchBack){
    FakePanel panel(4,4);
    std::array<uint16_t,16> scratch{};LogicalAdapter scaled(panel,{2,2,2,0,0,0},scratch.data(),scratch.size());
    Cache cache;FakeClock clock;Engine engine(cache,clock);
    struct Context {Engine* engine;Presenter* presenter=nullptr;unsigned repaints=0;} context{&engine};
    Presenter presenter(scaled,[](void* opaque){auto& c=*static_cast<Context*>(opaque);++c.repaints;
        const uint16_t px[4]={c.engine->scene.background,c.engine->scene.background,c.engine->scene.background,c.engine->scene.background};
        auto id=c.presenter->submit_logical({0,0,2,2},px,2);return id?c.presenter->committed(id):id.error;
    },&context);context.presenter=&presenter;
    uint16_t native=7;
    EXPECT_EQ(presenter.submit_native({3,3,4,4},&native,1).error,PresentError::Inactive);
    ASSERT_EQ(presenter.set_mode(PresentMode::Native),PresentError::None);
    auto id=presenter.submit_native({3,3,4,4},&native,1);ASSERT_TRUE(id);ASSERT_EQ(presenter.committed(id),PresentError::None);
    EXPECT_EQ(panel.pixels[15],7);EXPECT_EQ(panel.pixels[14],0x1234);
    Request q;q.meta.epoch=engine.epoch;q.meta.sequence=1;std::copy_n("native-fill",12,q.meta.id);q.count=1;q.operations[0].kind=Operation::Fill;q.operations[0].color=0x5555;
    ASSERT_EQ(engine.post(q,0).admission,Admission::Accepted);engine.tick(0);
    EXPECT_EQ(engine.scene.background,0x5555);EXPECT_EQ(panel.pixels[15],7);
    EXPECT_EQ(presenter.submit_logical({0,0,1,1},&native,1).error,PresentError::Inactive);
    ASSERT_EQ(presenter.set_mode(PresentMode::Logical),PresentError::None);EXPECT_EQ(context.repaints,1u);
    EXPECT_TRUE(std::all_of(panel.pixels.begin(),panel.pixels.end(),[](auto px){return px==0x5555;}));
    EXPECT_EQ(presenter.set_mode(PresentMode::Logical),PresentError::None);EXPECT_EQ(context.repaints,1u);
}
TEST(Present,CommitReleaseAndScanAreDistinctAndOnlyOverlappingLaterCommitSupersedes){
    FakePanel panel(4,4,true);LogicalAdapter adapter(panel,{4,4,1,0,0,0});Presenter presenter(adapter);
    std::vector<ScanOutcome> outcomes;ScanCallback cb{[](void* p,SubmitId,ScanOutcome result){static_cast<std::vector<ScanOutcome>*>(p)->push_back(result);},&outcomes};
    uint16_t data[4]={1,2,3,4};unsigned released=0;
    auto a=presenter.submit_logical({0,0,2,2},data,2,counter(released));ASSERT_TRUE(a);
    ASSERT_EQ(presenter.scan_fence(a,cb),PresentError::None);panel.scan();EXPECT_TRUE(outcomes.empty());
    ASSERT_EQ(presenter.wait_released(a),PresentError::None);panel.scan();EXPECT_TRUE(outcomes.empty());EXPECT_EQ(released,1u);
    ASSERT_EQ(presenter.committed(a),PresentError::None);
    auto separate=presenter.submit_logical({2,2,4,4},data,2);ASSERT_EQ(presenter.committed(separate),PresentError::None);
    auto overlap=presenter.submit_logical({1,1,3,3},data,2);ASSERT_EQ(presenter.scan_fence(overlap,cb),PresentError::None);ASSERT_EQ(presenter.committed(overlap),PresentError::None);
    EXPECT_TRUE(outcomes.empty());panel.scan();ASSERT_EQ(outcomes.size(),2u);
    EXPECT_EQ(outcomes[0],ScanOutcome::Superseded);EXPECT_EQ(outcomes[1],ScanOutcome::Presented);
    EXPECT_EQ(panel.events.front(),1u);EXPECT_EQ(panel.events[1],2u);EXPECT_EQ(panel.events[2],3u);
    auto later=presenter.submit_logical({1,1,3,3},data,2);ASSERT_EQ(presenter.committed(later),PresentError::None);panel.scan();EXPECT_EQ(outcomes.size(),2u);
}
TEST(Present,NonoverlappingCommitDoesNotSupersedeEarlierFence){
    FakePanel panel(4,4,true);uint16_t data=0;unsigned presented=0;
    auto a=panel.submit({0,0,1,1},&data,1);panel.scan_fence(a,{[](void* p,SubmitId,ScanOutcome outcome){EXPECT_EQ(outcome,ScanOutcome::Presented);++*static_cast<unsigned*>(p);},&presented});
    panel.committed(a);auto b=panel.submit({3,3,4,4},&data,1);panel.committed(b);panel.scan();EXPECT_EQ(presented,1u);
}
TEST(Present,InvalidGeometryBufferBudgetBackendAndModeErrorsAreReturned){
    FakePanel panel(800,800);uint16_t data=0;unsigned released=0;
    LogicalAdapter outside(panel,{360,360,2,81,40,0});EXPECT_EQ(outside.validate(),PresentError::Invalid);
    LogicalAdapter adapter(panel,{360,360,2,40,40,0});EXPECT_EQ(adapter.submit({0,0,1,1},&data,1,counter(released)).error,PresentError::Unavailable);
    EXPECT_EQ(adapter.submit({-1,0,1,1},&data,1).error,PresentError::Invalid);EXPECT_EQ(released,0u);
    LogicalAdapter identity(panel,{800,800,1,0,0,0});Presenter presenter(identity);
    EXPECT_EQ(presenter.submit_logical({0,0,1,1},nullptr,1).error,PresentError::Invalid);
    EXPECT_EQ(presenter.set_mode(static_cast<PresentMode>(99)),PresentError::Invalid);
    panel.fault=PresentError::Io;EXPECT_EQ(presenter.submit_logical({0,0,1,1},&data,1,counter(released)).error,PresentError::Io);EXPECT_EQ(released,0u);
    ASSERT_EQ(presenter.set_mode(PresentMode::Native),PresentError::None);EXPECT_EQ(presenter.set_mode(PresentMode::Logical),PresentError::Unavailable);EXPECT_EQ(presenter.mode(),PresentMode::Native);
}
TEST(Present,NullBackendCommitsWithoutAdvertisingScanOut){
    NullPanel panel({360,360,true,PixelFormat::Rgb565BE,360,true});uint16_t px=0;unsigned released=0;
    EXPECT_FALSE(panel.info().scan_fence);EXPECT_EQ(panel.submit({0,0,1,1},&px,1).error,PresentError::Unavailable);
    ASSERT_EQ(panel.init(),PresentError::None);auto id=panel.submit({0,0,1,1},&px,1,counter(released));ASSERT_TRUE(id);EXPECT_EQ(released,1u);
    EXPECT_EQ(panel.committed(id),PresentError::None);EXPECT_EQ(panel.committed({id.value+1}),PresentError::Invalid);
    EXPECT_EQ(panel.scan_fence(id,{}),PresentError::Unsupported);EXPECT_EQ(panel.set_backlight(50),PresentError::Unsupported);
}
TEST(Present,RejectsEveryOnePixelEdgeOverrunAndShortStride){
    FakePanel panel(4,4);LogicalAdapter adapter(panel,{4,4,1,0,0,0});Presenter presenter(adapter);
    uint16_t data[32]{};
    const Rect invalid[]={{0,0,5,1},{0,0,1,5},{-1,0,1,1},{0,-1,1,1}};
    for(auto r:invalid){
        EXPECT_FALSE(valid_native(r,data,8,panel.info()));
        EXPECT_EQ(presenter.submit_logical(r,data,8).error,PresentError::Invalid);
    }
    EXPECT_FALSE(valid_native({0,0,4,1},data,3,panel.info()));
    EXPECT_EQ(presenter.submit_logical({0,0,4,1},data,3).error,PresentError::Invalid);
    ASSERT_EQ(presenter.set_mode(PresentMode::Native),PresentError::None);
    for(auto r:invalid)EXPECT_EQ(presenter.submit_native(r,data,8).error,PresentError::Invalid);
    EXPECT_EQ(presenter.submit_native({0,0,4,1},data,3).error,PresentError::Invalid);
    EXPECT_TRUE(panel.transfers.empty());
}
TEST(Present,ScratchBudgetRejectsOnePixelShortWithoutWritingOrReleasing){
    FakePanel panel(4,4);std::array<uint16_t,8> scratch;scratch.fill(0xabcd);
    LogicalAdapter adapter(panel,{2,2,2,0,0,0},scratch.data(),3);uint16_t data=7;unsigned released=0;
    EXPECT_EQ(adapter.submit({0,0,1,1},&data,1,counter(released)).error,PresentError::Unavailable);
    EXPECT_EQ(released,0u);EXPECT_TRUE(panel.transfers.empty());
    EXPECT_TRUE(std::all_of(scratch.begin(),scratch.end(),[](auto px){return px==0xabcd;}));
}
TEST(Present,ScaledStripesWaitForDmaOwnershipBeforeReusingScratch){
    FakePanel panel(4,4);std::array<uint16_t,8> scratch{};
    LogicalAdapter adapter(panel,{2,2,2,0,0,0},scratch.data(),scratch.size());
    uint16_t first[]={1,2},second[]={3,4};
    auto a=adapter.submit({0,0,2,1},first,2);ASSERT_TRUE(a);EXPECT_FALSE(panel.transfers[0].release);
    auto b=adapter.submit({0,1,2,2},second,2);ASSERT_TRUE(b);EXPECT_TRUE(panel.transfers[0].release);
    ASSERT_EQ(panel.committed(a),PresentError::None);ASSERT_EQ(panel.committed(b),PresentError::None);
    const std::vector<uint16_t> expected={1,1,2,2,1,1,2,2,3,3,4,4,3,3,4,4};
    EXPECT_EQ(panel.pixels,expected);
}
TEST(Present,OddBorderHeightClipsLastScratchChunkToBorder){
    FakePanel panel(9,11);std::array<uint16_t,18> scratch{};
    LogicalAdapter adapter(panel,{2,1,2,2,7,0xbeef},scratch.data(),scratch.size());
    ASSERT_EQ(adapter.fill_border(),PresentError::None);
    for(int y=0;y<11;++y)for(int x=0;x<9;++x)
        EXPECT_EQ(panel.pixels[y*9+x],(x<2 || x>=6 || y<7 || y>=9)?0xbeef:0x1234)<<x<<","<<y;
}
TEST(Present,ModeSwitchDrainsLastCommitAndPreservesOwnerOnFailure){
    FakePanel panel(4,4);LogicalAdapter adapter(panel,{4,4,1,0,0,0});Presenter presenter(adapter);
    uint16_t data=7;auto id=presenter.submit_logical({0,0,1,1},&data,1);ASSERT_TRUE(id);
    panel.commit_fault=PresentError::Io;
    EXPECT_EQ(presenter.set_mode(PresentMode::Native),PresentError::Io);EXPECT_EQ(presenter.mode(),PresentMode::Logical);
    panel.commit_fault=PresentError::None;
    ASSERT_EQ(presenter.set_mode(PresentMode::Native),PresentError::None);
    EXPECT_TRUE(panel.transfers[0].commit);EXPECT_EQ(panel.commit_calls,2u);EXPECT_EQ(panel.pixels[0],7);
}
TEST(Present,ModeSwitchRestoresBordersRevertsFailedRepresentAndTracksNewestBorderId){
    FakePanel panel(8,8);panel.latest_only=true;std::array<uint16_t,16> scratch{};
    LogicalAdapter adapter(panel,{2,2,2,2,2,0xbeef},scratch.data(),scratch.size());
    struct Context {PresentError error=PresentError::Io;unsigned calls=0;} context;
    Presenter presenter(adapter,[](void* p){auto& c=*static_cast<Context*>(p);++c.calls;return c.error;},&context);
    EXPECT_EQ(presenter.info().w,8);EXPECT_EQ(presenter.info().stride_px,8u);
    ASSERT_EQ(presenter.set_mode(PresentMode::Native),PresentError::None);
    uint16_t data=7;auto id=presenter.submit_native({0,0,1,1},&data,1);ASSERT_TRUE(id);
    EXPECT_EQ(presenter.set_mode(PresentMode::Logical),PresentError::Io);EXPECT_EQ(presenter.mode(),PresentMode::Native);
    EXPECT_EQ(context.calls,1u);EXPECT_TRUE(panel.transfers[0].commit);
    context.error=PresentError::None;
    ASSERT_EQ(presenter.set_mode(PresentMode::Logical),PresentError::None);EXPECT_EQ(context.calls,2u);
    for(int y=0;y<8;++y)for(int x=0;x<8;++x)
        EXPECT_EQ(panel.pixels[y*8+x],(x<2 || x>=6 || y<2 || y>=6)?0xbeef:0x1234);
    EXPECT_EQ(presenter.set_mode(PresentMode::Native),PresentError::None);
}
