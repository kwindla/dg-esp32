#pragma once
#include <cstddef>
#include <cstdint>
namespace dg_display {
struct Pixel { uint16_t color=0; uint8_t alpha=255; };
uint16_t blend(uint16_t destination, Pixel source) noexcept;
class Image {
    const uint8_t* bytes_=nullptr; size_t size_=0; uint16_t width_=0,height_=0; uint8_t alpha_=0,codec_=0; uint32_t payload_=0;
    bool row(unsigned y,unsigned x,unsigned count,Pixel* out,bool full) const noexcept;
public:
    bool open(const uint8_t* bytes,size_t size) noexcept;
    bool crop(unsigned y,unsigned x,unsigned count,Pixel* out,size_t capacity) const noexcept;
    unsigned width() const noexcept{return width_;}
    unsigned height() const noexcept{return height_;}
    unsigned alpha() const noexcept{return alpha_;}
    unsigned codec() const noexcept{return codec_;}
    // The backing bytes must remain immutable for this view's entire lifetime.
};
}
