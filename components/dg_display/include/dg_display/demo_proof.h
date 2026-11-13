#pragma once
#include "scene.h"
#include "panel_config.h"
#include "demo_screen.h"
namespace dg_display {
// Match the exact completed scene, not merely a desired mailbox value. Asset
// lookups may return null while disabled/reconfigured; never dereference them.
template<class Cat,class Status> unsigned demo_scene_screen(const Scene& scene,Cat cat,Status status)noexcept{
    auto matches=[](const Layer& layer,ImageHandle* image,int x,int y){return image && layer.visible && layer.image==image && layer.x==x && layer.y==y;};
    auto extras_hidden=[&](unsigned first){for(unsigned i=first;i<8;++i)if(scene.layers[i].visible)return false;return true;};
    const auto& base=scene.layers[0];
    if(!base.image || base.image->width!=PanelWidth || base.image->height!=PanelHeight)return 99;
    if(matches(base,status(0),0,0) && extras_hidden(1))return unsigned(DemoScreen::Disconnected);
    if(matches(base,status(1),0,0) && extras_hidden(2)){
        for(unsigned i=2;i<5;++i)if(matches(scene.layers[1],status(i),144,285))return unsigned(DemoScreen::Connecting);
    }
    if(matches(base,cat(0),0,0) && extras_hidden(4)){
        auto feature=[&](unsigned layer,unsigned first,unsigned count,int x,int y){
            for(unsigned i=first;i<first+count;++i)if(matches(scene.layers[layer],cat(i),cat_coordinate(x),cat_coordinate(y)))return true;
            return false;
        };
        if(feature(1,1,6,168,192) && feature(2,7,6,152,176) && feature(3,13,3,92,180))return unsigned(DemoScreen::Live);
    }
    return 99;
}
}
