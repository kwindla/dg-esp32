#pragma once
namespace dg_display {
// Close an opened/attempted connection on early return or exception. Closing
// must not depend on the engine mutex. Explicit close disarms the destructor.
template<class Handle,class Close> class ConnectionScope {
public:
    ConnectionScope(Handle handle,Close close)noexcept:handle_(handle),close_(close){}
    ~ConnectionScope(){close();}
    void close()noexcept{if(active_){active_=false;close_(handle_);}}
    ConnectionScope(const ConnectionScope&)=delete;
    ConnectionScope& operator=(const ConnectionScope&)=delete;
private:
    Handle handle_;Close close_;bool active_=true;
};
}
