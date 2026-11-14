#include "pipecat/esp_peer/stat_json.h"
#include <gtest/gtest.h>
#include <cstdlib>
#include <atomic>
namespace {
std::atomic<unsigned> character_buffers{0};
}
namespace pipecat::esp_peer {
// Platform boundary only: run the production JSON/string allocator and dumper.
void* stat_allocate(size_t n) {
    // 256 characters and the terminator; libc++ rounds the capacity up to 16 bytes.
    if(n>=257 && n<=272)++character_buffers;
    return std::malloc(n);
}
void stat_free(void* p) {std::free(p);}
void stat_init() {}
}
using namespace pipecat::esp_peer;
TEST(StatJson, LongStringBufferUsesTheStatArenaNotTheGeneralHeap) {
    character_buffers=0;
    const std::string text(256,'x');
    const StatJson j=text;
    EXPECT_EQ(character_buffers.load(),1u);
    EXPECT_EQ(j.get<std::string>(),text);
    EXPECT_EQ(stat_dump(j),'"'+text+'"');
}
TEST(StatJson, TelemetryKeysAndStdStringsRoundTripWithoutSchemaChanges) {
    const std::string name="audio-playback";
    StatJson j={{"tasks",StatJson::array()},{"long_counter_field_name",42}};
    j["tasks"].push_back({{"name",name},{"stack_margin_percent",40.5},{"stack_free_bytes",7000}});
    j[StatString(std::string("dynamic_counter_key"))]=7;
    auto normal=nlohmann::json::parse(stat_dump(j));
    EXPECT_EQ(normal["tasks"][0]["name"],name);
    EXPECT_EQ(normal["tasks"][0]["stack_free_bytes"],7000);
    EXPECT_EQ(normal["long_counter_field_name"],42);
    EXPECT_EQ(StatJson::parse(normal.dump()),j);
}
