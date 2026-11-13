#pragma once
namespace dg {
// Never release the persistence/session guard until peer teardown succeeded.
template<class Disconnect,class EndDisplay>
void finish_demo_hold(Disconnect disconnect,EndDisplay end_display) {
    disconnect();end_display();
}
}
