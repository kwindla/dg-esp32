#include "dg_display/arena.h"
#include <new>
#include <algorithm>
namespace dg_display {
bool Arena::init(void* memory,size_t bytes) noexcept {
    if(memory_ || !memory || (uintptr_t(memory)&15) || bytes<sizeof(Block)+16)return false;
    memory_=static_cast<uint8_t*>(memory);budget_=bytes&~size_t(15);head_=new(memory_) Block;head_->size=budget_-sizeof(Block);return true;
}
void* Arena::allocate(size_t bytes) noexcept {
    if(!bytes || bytes>SIZE_MAX-15)return nullptr;
    bytes=(bytes+15)&~size_t(15);
    for(auto* b=head_;b;b=b->next)if(b->free && b->size>=bytes){
        if(b->size-bytes>=sizeof(Block)+16){auto* next=new(reinterpret_cast<uint8_t*>(b+1)+bytes) Block;next->size=b->size-bytes-sizeof(Block);next->next=b->next;b->next=next;b->size=bytes;}
        b->free=false;++allocations_;return b+1;
    }
    return nullptr;
}
void Arena::release(void* p) noexcept {
    for(auto* b=head_;b;b=b->next)if(b+1==p){b->free=true;break;}
    for(auto* b=head_;b && b->next;)if(b->free && b->next->free){b->size+=sizeof(Block)+b->next->size;b->next=b->next->next;}else b=b->next;
}
size_t Arena::charge(void* p) const noexcept {for(auto* b=head_;b;b=b->next)if(b+1==p && !b->free)return b->size+sizeof(Block);return 0;}
size_t Arena::used() const noexcept {size_t n=0;for(auto* b=head_;b;b=b->next)if(!b->free)n+=b->size+sizeof(Block);return n;}
size_t Arena::largest() const noexcept {size_t n=0;for(auto* b=head_;b;b=b->next)if(b->free)n=std::max(n,b->size);return n;}
}
