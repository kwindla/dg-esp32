#include "audio_slot.h"
#include <gtest/gtest.h>
#include <future>
#include <thread>
TEST(AudioSlotTest, UnpublishedAndClearedSlotsReturnNoObject) {
    AudioSlot<int> slot;
    {auto lease=slot.borrow();EXPECT_EQ(lease.pointer,nullptr);EXPECT_TRUE(slot.idle());}
    int value=7;slot.publish(&value);
    {auto lease=slot.borrow();ASSERT_EQ(lease.pointer,&value);EXPECT_EQ(*lease.pointer,7);}
    slot.clear();
    {auto lease=slot.borrow();EXPECT_EQ(lease.pointer,nullptr);EXPECT_TRUE(slot.idle());}
    EXPECT_TRUE(slot.idle());
}
TEST(AudioSlotTest, TeardownMustWaitForOutstandingReader) {
    AudioSlot<int> slot;int value=7;slot.publish(&value);
    std::promise<void> entered,release;auto release_future=release.get_future();
    std::thread reader([&]{auto lease=slot.borrow();entered.set_value();release_future.wait();EXPECT_EQ(*lease.pointer,7);});
    entered.get_future().wait();slot.clear();
    EXPECT_FALSE(slot.idle());
    {auto late=slot.borrow();EXPECT_EQ(late.pointer,nullptr);}
    EXPECT_FALSE(slot.idle());
    release.set_value();reader.join();EXPECT_TRUE(slot.idle());
}
TEST(AudioSlotTest, NextGenerationPublishesOnlyAfterReadersDrain) {
    AudioSlot<int> slot;int old_value=1,new_value=2;slot.publish(&old_value);
    {auto lease=slot.borrow();slot.clear();EXPECT_FALSE(slot.idle());EXPECT_EQ(*lease.pointer,1);}
    ASSERT_TRUE(slot.idle());slot.publish(&new_value);
    {auto lease=slot.borrow();EXPECT_EQ(*lease.pointer,2);}
}
