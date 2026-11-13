#include "pipecat/esp_peer/stat_json.h"
namespace pipecat::esp_peer {
std::string stat_dump(const StatJson& value) {
    // Fixed initial capacity prevents serialized counter growth from doubling
    // the system-heap allocation during an endurance measurement.
    std::string output;output.reserve(32768);
    nlohmann::detail::serializer<StatJson> serializer(nlohmann::detail::output_adapter<char>(output),' ');
    serializer.dump(value,false,false,0);return output;
}
}
