#pragma once
#include <cstdint>
#include <memory>
namespace dg {
class MediaSource {
public:
    virtual ~MediaSource()=default;
    virtual bool valid(uint64_t generation) const=0;
};
// Keeps the source alive through short writes and tests its current generation.
// No metadata lock is held while PCM is submitted to I2S.
struct MediaToken {
    std::shared_ptr<const MediaSource> source;
    uint64_t generation=0;
    bool valid() const {return source && source->valid(generation);}
    bool same(const MediaToken& other) const {return source==other.source && generation==other.generation;}
};
}
