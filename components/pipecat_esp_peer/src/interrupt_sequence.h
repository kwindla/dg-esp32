#pragma once
#include <cstdint>
#include <nlohmann/json.hpp>
namespace pipecat::esp_peer {
// Session-local monotonic IDs supplied by the demo bot. A delayed repeat remains
// a no-op even if BotStarted or a newer interrupt intervenes. Legacy notices
// without an ID use Playout's redundant-interrupt latch.
class InterruptSequence {
    uint64_t last_=0;
public:
    bool duplicate(const nlohmann::json& message) {
        if(message.value("type", "")!="bot-interrupted")return false;
        auto d=message.find("data");
        if(d==message.end() || !d->is_object())return false;
        auto id=d->find("interrupt_seq");
        if(id==d->end() || !id->is_number_unsigned())return false;
        auto value=id->get<uint64_t>();if(!value)return false;
        if(value<=last_)return true;
        last_=value;return false;
    }
};
}
