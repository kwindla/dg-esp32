#include "interrupt_sequence.h"
#include <gtest/gtest.h>
using namespace pipecat::esp_peer;
TEST(InterruptSequence, DelayedRepeatCannotCancelNewReplyOrNewerInterrupt) {
    InterruptSequence s;
    auto notice=[](unsigned n){return nlohmann::json{{"type","bot-interrupted"},{"data",{{"interrupt_seq",n}}}};};
    EXPECT_FALSE(s.duplicate(notice(1)));
    EXPECT_FALSE(s.duplicate({{"type","bot-started-speaking"}}));
    EXPECT_TRUE(s.duplicate(notice(1)));
    EXPECT_FALSE(s.duplicate(notice(2)));EXPECT_TRUE(s.duplicate(notice(1)));EXPECT_TRUE(s.duplicate(notice(2)));
    EXPECT_FALSE(s.duplicate({{"type","bot-interrupted"}}));
    EXPECT_FALSE(s.duplicate(notice(0)));
    InterruptSequence fresh;EXPECT_FALSE(fresh.duplicate(notice(1)));
}
