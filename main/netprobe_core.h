#pragma once
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
namespace dg::netprobe {
// Caller serializes access. No allocation in the tcpip mailbox-post wrapper.
// Closed port entries are recycled; aggregate totals survive reconnects.
class MailboxTracker {
public:
    struct Slot {void* box=nullptr;void* connection=nullptr;uint16_t port=0;};
    struct Port {uint16_t local_port=0;bool used=false;unsigned references=0;uint64_t full=0,last_us=0;};
    std::array<Slot,24> sockets{};
    std::array<Port,64> ports{};
    uint64_t full=0,last_us=0,untracked=0,untracked_ports=0;
    void add(void* box,void* connection) {
        for(auto& slot:sockets)if(!slot.box) {slot={box,connection};return;}
        ++untracked;
    }
    void remove(void* box) {for(auto& slot:sockets)if(slot.box==box) {
        for(auto& entry:ports)if(entry.used && entry.local_port==slot.port && entry.references)--entry.references;
        slot={};
    }}
    void* connection(void* box) const {for(auto& slot:sockets)if(slot.box==box)return slot.connection;return nullptr;}
    void bind(uint16_t port,void* box=nullptr) {
        auto* entry=find(port);if(!entry) {++untracked_ports;return;}
        if(box) {for(auto& slot:sockets)if(slot.box==box) {
            if(slot.port==port)return;
            slot.port=port;++entry->references;return;
        }} else ++entry->references;
    }
    void overflow(void* box,uint16_t port,uint64_t now) {
        if(!connection(box))return;
        ++full;last_us=now;
        if(auto* entry=find(port)) {++entry->full;entry->last_us=now;}else ++untracked_ports;
    }
private:
    Port* find(uint16_t port) {
        for(auto& entry:ports)if(entry.used && entry.local_port==port)return &entry;
        for(auto& entry:ports)if(!entry.used) {entry.local_port=port;entry.used=true;return &entry;}
        for(auto& entry:ports)if(!entry.references) {entry={};entry.local_port=port;entry.used=true;return &entry;}
        return nullptr;
    }
};
// Fixed run bitmap. Modular subtraction handles runs starting near UINT32_MAX.
// Refuse outside-window packets instead of aliasing them onto older sequences.
class SequenceBitmap {
public:
    SequenceBitmap(uint8_t* bits, size_t capacity, uint32_t base=0):bits_(bits),capacity_(capacity),base_(base) {std::memset(bits,0,(capacity+7)/8);}
    enum Result { New, Duplicate, Outside };
    Result add(uint32_t seq) {
        uint32_t offset=seq-base_;
        if(offset>=capacity_)return Outside;
        auto& byte=bits_[offset/8];uint8_t mask=1u<<(offset%8);
        if(byte&mask)return Duplicate;
        byte|=mask;++unique_;highest_=std::max(highest_,size_t(offset)+1);return New;
    }
    bool has(size_t offset) const {return offset<capacity_ && (bits_[offset/8]&(1u<<(offset%8)));}
    size_t unique() const {return unique_;}
    size_t highest() const {return highest_;}
    void bursts(size_t expected, uint32_t (&hist)[5]) const {
        std::memset(hist,0,sizeof hist);size_t length=0;
        for(size_t i=0;i<std::min(expected,capacity_);++i) {
            if(!has(i))++length;
            if(length && (has(i) || i+1==std::min(expected,capacity_))) {
                ++hist[length==1?0:length<=3?1:length<=10?2:length<=50?3:4];length=0;
            }
        }
    }
private:
    uint8_t* bits_;size_t capacity_,unique_=0,highest_=0;uint32_t base_;
};
struct Record {int64_t arrival_us;uint32_t sequence;uint16_t path,reserved;};
static_assert(sizeof(Record)==16);
constexpr size_t ring_capacity=46080; // 15.36 min at 50 pps, per path.
constexpr size_t bitmap_capacity=65536;
}
