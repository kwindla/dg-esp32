#pragma once
#include <atomic>
// One owner publishes/clears; audio callers borrow without a per-frame mutex.
// The owner must clear and wait for idle before destroying the pointed object.
template<class T> class AudioSlot {
    alignas(32) std::atomic<T*> pointer_{nullptr};
    std::atomic<unsigned> readers_{0};
public:
    class Lease {
        AudioSlot& owner_;
    public:
        T* const pointer;
        explicit Lease(AudioSlot& owner):owner_(owner),pointer(acquire(owner)) {}
        Lease(const Lease&)=delete;
        Lease& operator=(const Lease&)=delete;
        ~Lease() {if(pointer)--owner_.readers_;}
    private:
        static T* acquire(AudioSlot& owner) {
            T* pointer=owner.pointer_.load();
            if(!pointer)return nullptr;
            // Validate after registering. A reader delayed across clear()
            // never dereferences the removed object; null calls do not hold
            // up teardown. Sequential consistency orders all three steps.
            ++owner.readers_;
            if(owner.pointer_.load()!=pointer) {--owner.readers_;return nullptr;}
            return pointer;
        }
    };
    Lease borrow() {return Lease(*this);}
    void publish(T* pointer) {pointer_=pointer;}
    void clear() {pointer_=nullptr;}
    bool idle() const {return readers_.load()==0;}
};
