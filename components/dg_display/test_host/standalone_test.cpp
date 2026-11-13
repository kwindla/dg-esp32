#include <gtest/gtest.h>
#include "dg_touch_gesture.h"
#include "dg_display/demo_screen.h"
#include "dg_display/demo_proof.h"
using dg_board::DoubleTap;
namespace {
bool tap(DoubleTap& touch,uint32_t down,uint32_t up,bool enabled=true,unsigned x=100){
    EXPECT_FALSE(touch.sample(down,true,x,100,enabled));
    return touch.sample(up,false,x,100,enabled);
}
}
TEST(Touch,TwoShortTapsEmitOneRequestAndIgnoreHeldContact){
    DoubleTap touch;
    EXPECT_FALSE(tap(touch,100,150));
    EXPECT_FALSE(touch.sample(220,true,100,100,true));
    EXPECT_FALSE(touch.sample(240,true,100,100,true));
    EXPECT_TRUE(touch.sample(270,false,100,100,true));
    EXPECT_FALSE(touch.sample(280,false,100,100,true));
    EXPECT_EQ(touch.counts().taps,2u);EXPECT_EQ(touch.counts().double_taps,1u);
}
TEST(Touch,BounceDragHoldAndLatePairDoNotReconnect){
    DoubleTap bounce;EXPECT_FALSE(tap(bounce,10,15));EXPECT_FALSE(tap(bounce,40,45));
    DoubleTap drag;drag.sample(100,true,100,100,true);drag.sample(140,true,150,100,true);
    EXPECT_FALSE(drag.sample(160,false,150,100,true));EXPECT_FALSE(tap(drag,220,270));
    DoubleTap held;EXPECT_FALSE(tap(held,100,600));EXPECT_FALSE(tap(held,650,700));
    DoubleTap late;EXPECT_FALSE(tap(late,100,150));EXPECT_FALSE(tap(late,500,550));
    DoubleTap far;EXPECT_FALSE(tap(far,100,150));EXPECT_FALSE(tap(far,230,280,true,200));
}
TEST(Touch,ReconnectDiscardsTapsAndPreexistingPartialPair){
    DoubleTap touch;EXPECT_FALSE(tap(touch,100,150));
    EXPECT_FALSE(tap(touch,200,250,false));EXPECT_FALSE(tap(touch,300,350,false));
    EXPECT_EQ(touch.counts().ignored,2u);
    EXPECT_FALSE(tap(touch,400,450));EXPECT_TRUE(tap(touch,500,550));
    touch.cancel();EXPECT_FALSE(tap(touch,600,650));
}
TEST(Touch,ReadFailureInvalidatesPartialPairAndMillisecondWrapWorks){
    DoubleTap touch;EXPECT_FALSE(tap(touch,100,150));touch.cancel();EXPECT_FALSE(tap(touch,200,250));
    DoubleTap wrap;EXPECT_FALSE(tap(wrap,0xffffff80u,0xffffffb0u));EXPECT_TRUE(tap(wrap,20,60));
}
TEST(DemoScreens,ConnectingPulsesWithoutCatchupAndLiveWaitsForExplicitReady){
    using namespace dg_display;DemoAnimation animation;
    auto f=animation.tick(DemoScreen::Disconnected,0);EXPECT_EQ(f.screen,DemoScreen::Disconnected);
    f=animation.tick(DemoScreen::Connecting,100);EXPECT_EQ(f.dots,0u);
    EXPECT_EQ(animation.tick(DemoScreen::Connecting,449).dots,0u);
    EXPECT_EQ(animation.tick(DemoScreen::Connecting,450).dots,1u);
    f=animation.tick(DemoScreen::Connecting,10000);EXPECT_EQ(f.screen,DemoScreen::Connecting);EXPECT_EQ(f.dots,2u);
    EXPECT_EQ(animation.tick(DemoScreen::Connecting,10001).dots,2u);
    EXPECT_EQ(animation.tick(DemoScreen::Live,10100).screen,DemoScreen::Live);
    EXPECT_EQ(animation.tick(DemoScreen::Disconnected,10200).screen,DemoScreen::Disconnected);
    EXPECT_EQ(animation.tick(DemoScreen::Connecting,10500).dots,0u);
}

TEST(DemoScreens,ProofRejectsWrongAssetsPositionsAndOverlays){
    using namespace dg_display;Scene scene;ImageHandle cat[16]{},status[5]{};
    cat[0].width=status[0].width=status[1].width=PanelWidth;
    cat[0].height=status[0].height=status[1].height=PanelHeight;
    auto classify=[&]{return demo_scene_screen(scene,[&](unsigned i){return &cat[i];},[&](unsigned i){return &status[i];});};
    scene.layers[0]={&status[0],0,0,true};EXPECT_EQ(classify(),0u);
    scene.layers[7]={&cat[1],0,0,true};EXPECT_EQ(classify(),99u);scene.layers[7].visible=false;
    scene.layers[0]={&status[1],0,0,true};scene.layers[1]={&status[2],144,285,true};EXPECT_EQ(classify(),1u);
    scene.layers[1].x=145;EXPECT_EQ(classify(),99u);scene.layers[1].x=144;
    scene.layers[2]={&cat[7],0,0,true};EXPECT_EQ(classify(),99u);scene.layers[2].visible=false;
    scene.layers[0]={&cat[0],0,0,true};
    scene.layers[1]={&cat[1],int16_t(cat_coordinate(168)),int16_t(cat_coordinate(192)),true};
    scene.layers[2]={&cat[8],int16_t(cat_coordinate(152)),int16_t(cat_coordinate(176)),true};
    scene.layers[3]={&cat[13],int16_t(cat_coordinate(92)),int16_t(cat_coordinate(180)),true};EXPECT_EQ(classify(),2u);
    scene.layers[2].image=&cat[1];EXPECT_EQ(classify(),99u);scene.layers[2].image=&cat[8];
    scene.layers[0].x=8;EXPECT_EQ(classify(),99u);scene.layers[0].x=0;
    scene.layers[4].visible=true;EXPECT_EQ(classify(),99u);scene.layers[4].visible=false;
    EXPECT_EQ(demo_scene_screen(scene,[](unsigned)->ImageHandle*{return nullptr;},[](unsigned)->ImageHandle*{return nullptr;}),99u);
}
