#pragma once
#include "dgi.h"
#include <cstdint>
#include <cstddef>
namespace dg_display {
struct Rect { int32_t x0=0,y0=0,x1=0,y1=0; bool empty()const{return x0>=x1 || y0>=y1;} };
Rect clip_align(Rect r) noexcept;
Rect dma_align(Rect r) noexcept;
Rect unite(Rect a,Rect b) noexcept;
struct Dirty {
    Rect rectangles[8]{}; unsigned count=0;
    void add(Rect r) noexcept;
    void clear() noexcept{count=0;}
};
enum class Pin:uint8_t {Visible,Scheduled,Rendering,Inflight,Session};
struct ImageHandle {
    void* context=nullptr; unsigned width=0,height=0;
    bool (*decode)(void*,unsigned,unsigned,unsigned,Pixel*,size_t)=nullptr;
    // Optional ownership hooks permit future raster/text handles without cache casts.
    void* reference=nullptr;bool (*retain)(void*,Pin)=nullptr;void (*release)(void*,Pin)=nullptr;
};
struct Layer { ImageHandle* image=nullptr; int16_t x=0,y=0; bool visible=false; };
struct Scene { Layer layers[8]{}; uint16_t background=0; uint64_t revision=0; char utterance[33]{}; };
Rect bounds(Layer layer) noexcept;
// Stripe/crop composition has no allocation or file IO. Scratch is caller-owned.
bool compose(const Scene& scene,Rect stripe,uint8_t* rgb565,size_t capacity,Pixel* scratch,size_t pixels) noexcept;
}
