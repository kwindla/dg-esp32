#include "dg_display/panel_config.h"
#include "dg_display/scene.h"
#include <algorithm>
namespace dg_display {
Rect clip_align(Rect r) noexcept {
    r.x0=std::max(int32_t(0),std::min(int32_t(PanelWidth),r.x0));r.x1=std::max(int32_t(0),std::min(int32_t(PanelWidth),r.x1));
    r.y0=std::max(int32_t(0),std::min(int32_t(PanelHeight),r.y0));r.y1=std::max(int32_t(0),std::min(int32_t(PanelHeight),r.y1));
    if(r.empty())return {};
    r.x0=r.x0/PanelXAlign*PanelXAlign;r.x1=(r.x1+PanelXAlign-1)/PanelXAlign*PanelXAlign;return r;
}
Rect dma_align(Rect r) noexcept {
    r=clip_align(r);
    if(!r.empty()){r.x0=r.x0/DmaXAlign*DmaXAlign;r.x1=std::min(int32_t(PanelWidth),(r.x1+DmaXAlign-1)/DmaXAlign*DmaXAlign);}
    if(!r.empty() && DmaYEvenIfRow8 && ((r.x1-r.x0)*2)%16){r.y0&=~int32_t(1);r.y1=std::min(int32_t(PanelHeight),(r.y1+1)&~int32_t(1));}
    return r;
}
Rect unite(Rect a,Rect b) noexcept {
    if(a.empty())return b;
    if(b.empty())return a;
    return {std::min(a.x0,b.x0),std::min(a.y0,b.y0),std::max(a.x1,b.x1),std::max(a.y1,b.y1)};
}
void Dirty::add(Rect r) noexcept {
    r=clip_align(r);if(r.empty())return;
    for(unsigned i=0;i<count;){auto a=rectangles[i];if(r.x0<=a.x1 && r.x1>=a.x0 && r.y0<=a.y1 && r.y1>=a.y0){r=unite(r,a);rectangles[i]=rectangles[--count];i=0;}else ++i;}
    if(count==8){for(unsigned i=0;i<count;++i)r=unite(r,rectangles[i]);count=0;}
    rectangles[count++]=r;
}
Rect bounds(Layer l) noexcept {
    if(!l.visible || !l.image || !l.image->width || !l.image->height || l.image->width>PanelWidth || l.image->height>PanelHeight)return {};
    // Positions are int16 and dimensions are bounded to PanelWidth, so sums fit int32.
    return {l.x,l.y,int32_t(l.x)+int32_t(l.image->width),int32_t(l.y)+int32_t(l.image->height)};
}
bool compose(const Scene& scene,Rect r,uint8_t* output,size_t capacity,Pixel* scratch,size_t scratch_count) noexcept {
    if(r.empty() || r.x0<0 || r.y0<0 || r.x1>PanelWidth || r.y1>PanelHeight || !output || !scratch)return false;
    size_t width=size_t(r.x1-r.x0),height=size_t(r.y1-r.y0);
    if(width>PanelWidth || height>PanelHeight || width*height>capacity/2 || scratch_count<width)return false;
    for(size_t i=0;i<width*height;++i){output[2*i]=scene.background>>8;output[2*i+1]=scene.background&255;}
    for(const auto& layer:scene.layers){
        Rect b=bounds(layer);if(b.empty() || !layer.image->decode)continue;
        int x0=std::max(r.x0,b.x0),x1=std::min(r.x1,b.x1),y0=std::max(r.y0,b.y0),y1=std::min(r.y1,b.y1);
        if(x0>=x1 || y0>=y1)continue;
        for(int y=y0;y<y1;++y){
            if(!layer.image->decode(layer.image->context,unsigned(y-layer.y),unsigned(x0-layer.x),unsigned(x1-x0),scratch,scratch_count))return false;
            size_t at=(size_t(y-r.y0)*width+size_t(x0-r.x0))*2;
            for(int x=x0;x<x1;++x,at+=2){uint16_t before=uint16_t(output[at])<<8|output[at+1];uint16_t after=blend(before,scratch[x-x0]);output[at]=after>>8;output[at+1]=after&255;}
        }
    }
    return true;
}
}
