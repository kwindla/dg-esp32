#include "netprobe_core.h"
#include <gtest/gtest.h>
using dg::netprobe::SequenceBitmap;
TEST(Netprobe, FreshEphemeralPortsRecycleAcrossHundredsOfReconnects) {
    dg::netprobe::MailboxTracker tracker;int box=0,conn=0;
    for(unsigned n=0;n<300;++n) {
        tracker.add(&box,&conn);tracker.bind(47000+n,&box);
        tracker.overflow(&box,47000+n,n);tracker.remove(&box);
    }
    EXPECT_EQ(tracker.full,300u);EXPECT_EQ(tracker.untracked_ports,0u);
}
TEST(Netprobe, MailboxReuseKeepsPortTotalsAndReleasesSlots) {
    dg::netprobe::MailboxTracker tracker;
    int box=0,conn=0;
    for(unsigned n=0;n<100;++n) {
        tracker.add(&box,&conn);tracker.bind(47000+n%2);
        tracker.overflow(&box,47000+n%2,n);tracker.remove(&box);
        EXPECT_EQ(tracker.connection(&box),nullptr);
    }
    EXPECT_EQ(tracker.full,100u);EXPECT_EQ(tracker.untracked,0u);
    EXPECT_EQ(tracker.ports[0].full,50u);EXPECT_EQ(tracker.ports[1].full,50u);
    tracker.overflow(&box,47000,1000);EXPECT_EQ(tracker.full,100u);
}
TEST(Netprobe, ExhaustedMailboxTrackingIsExplicit) {
    dg::netprobe::MailboxTracker tracker;int box[25]{},conn[25]{};
    for(unsigned n=0;n<25;++n)tracker.add(&box[n],&conn[n]);
    EXPECT_EQ(tracker.untracked,1u);
    tracker.remove(&box[0]);tracker.add(&box[24],&conn[24]);
    EXPECT_EQ(tracker.connection(&box[24]),&conn[24]);
    for(unsigned n=0;n<65;++n)tracker.bind(n);
    EXPECT_EQ(tracker.untracked_ports,1u);
}
TEST(Netprobe, ClosingAddressReuseCannotEraseNewRegistration) {
    dg::netprobe::MailboxTracker tracker;int box=0,old_conn=0,new_conn=0;
    tracker.add(&box,&old_conn);tracker.overflow(&box,50000,1);
    // The wrapper removes before freeing. Allocation at the same address
    // during deletion is a new registration, which is left untouched.
    tracker.remove(&box);tracker.add(&box,&new_conn);
    EXPECT_EQ(tracker.connection(&box),&new_conn);
    tracker.overflow(&box,50001,2);
    EXPECT_EQ(tracker.ports[0].full,1u);EXPECT_EQ(tracker.ports[1].full,1u);
    tracker.remove(&box);tracker.add(&box,&new_conn); // failed delete restoration
    EXPECT_EQ(tracker.connection(&box),&new_conn);
    EXPECT_EQ(tracker.full,2u);
}
TEST(Netprobe, DuplicateAndOutsideDoNotAlias) {
    uint8_t bits[2];SequenceBitmap seq(bits,16);
    EXPECT_EQ(seq.add(0),SequenceBitmap::New);EXPECT_EQ(seq.add(0),SequenceBitmap::Duplicate);
    EXPECT_EQ(seq.add(16),SequenceBitmap::Outside);EXPECT_EQ(seq.unique(),1u);
    EXPECT_FALSE(seq.has(16));EXPECT_EQ(seq.add(15),SequenceBitmap::New);
}
TEST(Netprobe, WrapAndReorder) {
    uint8_t bits[2];SequenceBitmap seq(bits,16,UINT32_MAX-2);
    EXPECT_EQ(seq.add(0),SequenceBitmap::New);EXPECT_EQ(seq.add(UINT32_MAX),SequenceBitmap::New);
    EXPECT_EQ(seq.add(UINT32_MAX-2),SequenceBitmap::New);EXPECT_EQ(seq.highest(),4u);
    EXPECT_TRUE(seq.has(3));EXPECT_EQ(seq.add(UINT32_MAX-3),SequenceBitmap::Outside);
}
TEST(Netprobe, BurstBucketsAndTrailingLoss) {
    uint8_t bits[16];SequenceBitmap seq(bits,128);unsigned cursor=0;
    for(unsigned length:{1,2,4,11,51}) {cursor+=length;seq.add(cursor++);}
    uint32_t hist[5];seq.bursts(cursor+3,hist);
    EXPECT_EQ(hist[0],1u);EXPECT_EQ(hist[1],2u);EXPECT_EQ(hist[2],1u);EXPECT_EQ(hist[3],1u);EXPECT_EQ(hist[4],1u);
}
