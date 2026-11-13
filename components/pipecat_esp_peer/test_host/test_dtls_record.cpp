#include <gtest/gtest.h>
#include "dtls_record.h"
using namespace pipecat::esp_peer;
TEST(DtlsRecord,ApplicationIdentityAndShortDatagrams) {
    uint8_t p[]={0x17,0xfe,0xfd,0x12,0x34,1,2,3,4,5,6,0,0};DtlsRecordIdentity id;
    EXPECT_TRUE(dtls_application(p,sizeof(p),id));EXPECT_TRUE(id.complete);
    EXPECT_EQ(id.epoch,0x1234);EXPECT_EQ(id.sequence,0x010203040506ULL);
    EXPECT_TRUE(dtls_application(p,1,id));EXPECT_FALSE(id.complete);
    EXPECT_FALSE(dtls_application(nullptr,0,id));p[0]=0x16;
    EXPECT_FALSE(dtls_application(p,sizeof(p),id));p[0]=0x80;
    EXPECT_FALSE(dtls_application(p,sizeof(p),id));
}
