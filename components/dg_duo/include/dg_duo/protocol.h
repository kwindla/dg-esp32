#pragma once
#include <atomic>
#include <cstdint>
namespace dg_duo {
enum class Persona:uint8_t {Angel,Devil};
inline const char* name(Persona c){return c==Persona::Angel?"angel":"devil";}
enum class Op:uint8_t {Hello,Toggle};
struct ClientMessage {uint32_t generation=0,seq=0;char session[33]{};Op op=Op::Hello;Persona have=Persona::Angel;};
struct Outcome {char session[129]{},conn[129]{};uint32_t seq=0;uint64_t rev=0;Persona character=Persona::Angel;bool state=true;char reason[33]{};};
enum class Fault:uint8_t {Malformed,Session,Connection,Baseline,Stale,Mailbox,Send,Timeout,Render,SwitchFailed,Mismatch,ReplayStale,Voice,Deadline,Cancelled,Other,Count};
const char* fault_name(Fault);
struct Counts {uint32_t toggles=0,retransmitted=0,resyncs=0,answered=0,displayed=0,taps_ignored=0,taps_injected=0,taps_physical=0,render_busy=0;uint32_t errors[unsigned(Fault::Count)]{};};
// One producer and one consumer. Neither waits; stale work is drained, never sent.
class Mailbox {
    std::atomic<unsigned> state_{0};ClientMessage item_;
public:
    bool post(const ClientMessage& m)noexcept;
    bool take(ClientMessage& m)noexcept;
    static bool current(const ClientMessage& m,uint32_t client,uint32_t app,bool ready)noexcept{
        return ready && m.generation==client && client==app;
    }
};
// App-control is the sole owner. Logical acknowledgement and render commit are separate.
class Device {
public:
    using Send=bool(*)(void*,const ClientMessage&);
    Device(uint64_t boot,Persona initial,Send send,void* context);
    void connect(uint32_t generation,int64_t now);
    void disconnect(uint32_t generation);
    void tap(int64_t now);
    void tick(int64_t now);
    bool receive(const Outcome&,uint32_t generation,int64_t now=0); // true only for a new applied state
    void error(Fault f){++counts.errors[unsigned(f)];}
    Persona character()const{return character_;}
    uint64_t revision()const{return rev_;}
    uint32_t generation()const{return generation_;}
    const char* session()const{return pending_.session;}
    const char* connection()const{return conn_;}
    bool pending()const{return awaiting_ || hello_retry_;}
    bool baseline()const{return conn_[0]!=0;}
    const ClientMessage& request()const{return pending_;}
    Counts counts;
private:
    Send send_;void* context_;ClientMessage pending_;char conn_[129]{};
    uint64_t boot_,rev_=0;uint32_t generation_=0,seq_=0;Persona character_;
    int64_t due_=0;unsigned attempts_=0,hello_errors_=0;bool hello_retry_=false,awaiting_=false,live_=false,resync_=false,waiting_connect_=false;
    void begin(Op,int64_t,bool resync=false);
};
// Persistence owns only a coalesced value. Call the store through the display's
// idle_configuration_write gate; a failed/busy store leaves the value dirty.
class DeferredCharacter {
    std::atomic<unsigned> wanted_{0},saved_{0};
public:
    void acknowledge(Persona c){wanted_.store(unsigned(c)+1);}
    bool pending()const{return wanted_.load()!=saved_.load();}
    template<class Store> bool flush(bool session,Store store){
        auto value=wanted_.load();if(session || value==saved_.load())return false;
        if(!store(Persona(value-1)))return false;
        saved_=value;return true;
    }
};
}
