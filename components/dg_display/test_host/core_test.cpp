#include "dg_display/panel_config.h"
#include "dg_display/engine.h"
#include "dg_display/flash_store.h"
#include <gtest/gtest.h>
#include <array>
#include <vector>
#include <string>
#include <fstream>
#include <filesystem>
#include <cstring>
#include <cstdlib>
using namespace dg_display;
namespace {
void put16(std::vector<uint8_t>& b,size_t p,uint16_t v){b[p]=v;b[p+1]=v>>8;}
void put32(std::vector<uint8_t>& b,size_t p,uint32_t v){for(unsigned i=0;i<4;++i)b[p+i]=v>>(8*i);}
std::vector<uint8_t> raw(unsigned w=1,unsigned h=1,uint16_t color=0xf800,unsigned alpha=255){unsigned mode=alpha==255?0:2;size_t row=1+w*(mode?3:2),payload=32+4*(h+1);std::vector<uint8_t> b(payload+h*row);std::memcpy(b.data(),"DGI1",4);b[4]=1;b[5]=mode;b[6]=1;put16(b,8,w);put16(b,10,h);put32(b,12,b.size());put32(b,16,32);put32(b,20,payload);for(unsigned y=0;y<=h;++y)put32(b,32+4*y,y*row);for(unsigned y=0;y<h;++y){size_t p=payload+y*row+1;for(unsigned x=0;x<w;++x){b[p++]=color>>8;b[p++]=color;}if(mode)for(unsigned x=0;x<w;++x)b[p++]=alpha;}return b;}
struct Fixture:testing::Test {
    alignas(16) std::array<uint8_t,65536> memory{};Cache cache;FakeClock clock;Engine engine{cache,clock};unsigned sequence=0;
    void SetUp()override{ASSERT_TRUE(cache.arena.init(memory.data(),memory.size()));clock.spans[0]={0,96000,1000000,FakeClock::Kind::Audio};clock.count=1;}
    Entry* load(uint16_t color=0xf800,bool session=false,unsigned w=1,unsigned h=1){auto b=raw(w,h,color);char hash[65];EXPECT_TRUE(digest(b.data(),b.size(),hash));Entry* e=nullptr;EXPECT_EQ(cache.reserve(hash,b.size(),e),Error::None);if(e->valid)return e;std::memcpy(e->bytes,b.data(),b.size());Image i;EXPECT_TRUE(i.open(e->bytes,e->size));EXPECT_EQ(cache.finish(*e,i,session),Error::None);return e;}
    Request show(Entry* e,unsigned layer=0,TimeKind time=TimeKind::Immediate,uint32_t timestamp=0){Request q;q.meta.epoch=engine.epoch;q.meta.sequence=++sequence;std::snprintf(q.meta.id,33,"req-%u",sequence);q.count=1;q.operations[0].kind=Operation::Show;q.operations[0].layer=layer;std::strcpy(q.operations[0].hash,e->hash);q.time=time;q.timestamp=timestamp;q.clock_epoch=clock.epoch;if(time!=TimeKind::Immediate)std::strcpy(q.utterance,"utterance");return q;}
    Request control(RequestKind kind,const char* hash=nullptr){Request q;q.meta.epoch=engine.epoch;q.meta.sequence=++sequence;q.kind=kind;std::snprintf(q.meta.id,33,"req-%u",sequence);if(hash)std::strcpy(q.hash,hash);return q;}
};
TEST(Arena,RejectBeforeAllocationAndCoalesce){alignas(16) uint8_t bytes[1024];Arena a;EXPECT_FALSE(a.init(bytes+1,100));ASSERT_TRUE(a.init(bytes,1024));auto initial=a.largest();EXPECT_EQ(a.allocate(SIZE_MAX),nullptr);EXPECT_EQ(a.allocation_calls(),0u);void* x=a.allocate(33);void* y=a.allocate(80);EXPECT_EQ(uintptr_t(x)%16,0u);EXPECT_GT(a.used(),113u);a.release(x);a.release(y);EXPECT_EQ(a.used(),0u);EXPECT_EQ(a.largest(),initial);}
TEST_F(Fixture,BudgetRejectionDoesNotAllocateOrEvict){auto* e=load();char hash[65];std::memset(hash,'1',64);hash[64]=0;auto calls=cache.arena.allocation_calls();Entry* result=nullptr;EXPECT_EQ(cache.reserve(hash,65536,result),Error::RamBudget);EXPECT_EQ(calls,cache.arena.allocation_calls());EXPECT_EQ(cache.find(e->hash),e);}
TEST_F(Fixture,PinsProtectLruAndCountMultipleReferences){auto* e=load();ASSERT_TRUE(cache.pin(*e,Pin::Visible));ASSERT_TRUE(cache.pin(*e,Pin::Visible));EXPECT_EQ(e->references(),2u);EXPECT_EQ(cache.erase(e->hash),Error::InUse);cache.unpin(*e,Pin::Visible);EXPECT_EQ(cache.erase(e->hash),Error::InUse);cache.unpin(*e,Pin::Visible);EXPECT_EQ(cache.erase(e->hash),Error::None);}
TEST_F(Fixture,LruEvictsOnlyUnpinned){alignas(16) uint8_t small[256];Cache c;ASSERT_TRUE(c.arena.init(small,256));auto fill=[&](unsigned v){auto b=raw(1,1,uint16_t(v));char hash[65];digest(b.data(),b.size(),hash);Entry* e=nullptr;EXPECT_EQ(c.reserve(hash,b.size(),e),Error::None);std::memcpy(e->bytes,b.data(),b.size());Image i;i.open(e->bytes,e->size);c.finish(*e,i,false);return e;};auto* pinned=fill(1);auto* old=fill(2);std::string saved=old->hash;c.pin(*pinned,Pin::Scheduled);fill(3);fill(4);EXPECT_EQ(c.find(saved.c_str()),nullptr);EXPECT_TRUE(pinned->valid);}
TEST_F(Fixture,FailedBatchReleasesAllReservationsAndPreservesScene){auto* e=load();auto q=show(e);q.count=2;q.operations[1]=q.operations[0];std::memset(q.operations[1].hash,'0',64);EXPECT_EQ(engine.post(q,0).error,Error::NotLoaded);EXPECT_EQ(e->references(),0u);EXPECT_EQ(engine.accepted_revision,0u);EXPECT_EQ(engine.occupancy(),0u);}
TEST_F(Fixture,AtomicBatchAndExpandedComposition){auto* red=load(0xf800,false,3,3);auto* blue=load(0x001f,false,2,2);auto q=show(red);q.count=2;q.operations[1]=q.operations[0];q.operations[1].layer=1;q.operations[1].x=2;std::strcpy(q.operations[1].hash,blue->hash);ASSERT_EQ(engine.post(q,0).admission,Admission::Accepted);EXPECT_FALSE(engine.scene.layers[0].visible);engine.tick(0);EXPECT_TRUE(engine.scene.layers[0].visible);EXPECT_TRUE(engine.scene.layers[1].visible);EXPECT_EQ(engine.accepted_revision,1u);RenderLease lease;ASSERT_TRUE(engine.snapshot(lease));uint8_t out[8*4*2];Pixel scratch[8];ASSERT_TRUE(compose(lease.scene,{0,0,8,4},out,sizeof(out),scratch,8));EXPECT_EQ(out[0],0xf8);EXPECT_EQ(out[2*2+1],0x1f);EXPECT_EQ(out[2*4],0);engine.complete(lease,true,32);EXPECT_EQ(engine.completed_revision,1u);}
TEST_F(Fixture,HideKeepsRenderingSnapshotPinned){auto* e=load();auto q=show(e);engine.post(q,0);engine.tick(0);RenderLease lease;engine.snapshot(lease);auto hide=control(RequestKind::Scene);hide.count=1;hide.operations[0].kind=Operation::Hide;engine.post(hide,0);engine.tick(0);EXPECT_EQ(cache.erase(e->hash),Error::InUse);engine.complete(lease,true,1);EXPECT_EQ(cache.erase(e->hash),Error::None);}
TEST_F(Fixture,DuplicateDoesNotRepeatAndReuseFails){auto q=show(load());auto first=engine.post(q,0);EXPECT_EQ(first.admission,Admission::Accepted);EXPECT_TRUE(engine.post(q,0).duplicate);EXPECT_EQ(engine.occupancy(),1u);q.operations[0].x=1;EXPECT_EQ(engine.post(q,0).error,Error::IdReuse);}
TEST_F(Fixture,OldReplayAfterResultEvictionIsStale){auto original=control(RequestKind::Delete,load()->hash);engine.post(original,0);engine.terminal(*engine.next_job(),Error::None);for(unsigned i=0;i<40;++i){auto q=control(RequestKind::Delete,original.hash);engine.post(q,0);engine.terminal(*engine.next_job(),Error::None);}EXPECT_EQ(engine.post(original,0).error,Error::StaleSequence);}
TEST_F(Fixture,FullQueueKeepsCancelAvailable){auto* e=load();for(unsigned i=0;i<16;++i){auto q=show(e,0,TimeKind::Rtp,48000+i);ASSERT_EQ(engine.post(q,0).admission,Admission::Accepted);}auto extra=show(e,0,TimeKind::Rtp,48020);EXPECT_EQ(engine.post(extra,0).admission,Admission::Full);EXPECT_EQ(e->pins[1],16u);auto cancel=control(RequestKind::Cancel);std::strcpy(cancel.utterance,"utterance");EXPECT_EQ(engine.post(cancel,0).admission,Admission::Accepted);EXPECT_EQ(engine.occupancy(),0u);EXPECT_EQ(e->references(),0u);EXPECT_EQ(engine.post(show(e,0,TimeKind::Rtp,48020),0).error,Error::Cancelled);}
TEST_F(Fixture,ImmediateSupersedesWholeOverlappingBatch){auto* e=load();auto q=show(e,0,TimeKind::Rtp,48000);q.count=2;q.operations[1]=q.operations[0];q.operations[1].layer=1;engine.post(q,0);auto replace=show(e,0);ASSERT_EQ(engine.post(replace,0).admission,Admission::Accepted);EXPECT_EQ(engine.result(q.meta.id)->state,ResultState::Superseded);EXPECT_EQ(engine.superseded,1u);EXPECT_EQ(e->pins[1],1u);engine.tick(0);EXPECT_FALSE(engine.scene.layers[1].visible);}
TEST_F(Fixture,CancelInvalidatesInFlightBetweenStripes){auto* e=load();auto q=show(e);std::strcpy(q.utterance,"utterance");engine.post(q,0);engine.tick(0);RenderLease lease;engine.snapshot(lease);auto cancel=control(RequestKind::Cancel);std::strcpy(cancel.utterance,"utterance");engine.post(cancel,0);EXPECT_FALSE(engine.current(lease));engine.complete(lease,false,100);EXPECT_EQ(engine.rendered,0u);EXPECT_EQ(engine.pixels,100u);EXPECT_EQ(engine.dirty.count,1u);}
TEST_F(Fixture,SessionPinsSurviveReconnectAndExplicitDeleteReleases){auto* e=load(0xf800,true);std::string hash=e->hash;engine.start_session();engine.transport_down();engine.start_session();engine.end_session();EXPECT_EQ(e->pins[4],1u);EXPECT_EQ(cache.erase(hash.c_str()),Error::None);}
TEST_F(Fixture,EpochRejectsOldRequestAndClearsOldScene){auto q=show(load());engine.post(q,0);engine.tick(0);engine.start_session();EXPECT_EQ(engine.post(q,0).error,Error::StaleEpoch);EXPECT_FALSE(engine.scene.layers[0].visible);}
TEST_F(Fixture,MalformedTypedRequestCannotReadPastStrings){auto q=show(load());std::memset(q.operations[0].hash,'a',65);EXPECT_EQ(engine.post(q,0).error,Error::Malformed);q=show(load());q.operations[0].layer=8;EXPECT_EQ(engine.post(q,0).error,Error::Malformed);q=show(load());q.meta.version=2;EXPECT_EQ(engine.post(q,0).error,Error::Version);}
TEST_F(Fixture,DueTimeSubtractsOnlyRenderLatencyAndExplicitLead){auto q=show(load(),0,TimeKind::Rtp,48000);engine.render_latency_us=30000;engine.visual_lead_us=10000;ASSERT_EQ(engine.post(q,0).admission,Admission::Accepted);engine.tick(1959999);EXPECT_FALSE(engine.scene.layers[0].visible);engine.tick(1960000);EXPECT_TRUE(engine.scene.layers[0].visible);}
TEST_F(Fixture,FutureTimelineSurvivesLaterArrival){auto* e=load();auto first=show(e,0,TimeKind::Rtp,48000);auto future=show(e,0,TimeKind::Rtp,72000);engine.post(first,0);engine.post(future,500000);engine.tick(1970000);EXPECT_EQ(engine.occupancy(),1u);engine.tick(2470000);EXPECT_EQ(engine.occupancy(),0u);EXPECT_EQ(engine.accepted_revision,2u);}
TEST_F(Fixture,UnknownArrivalStaleAndUncertainNeverBecomeImmediate){auto* e=load();auto q=show(e,0,TimeKind::Arrival,0);EXPECT_EQ(engine.post(q,0).error,Error::Expired);q=show(e,0,TimeKind::Rtp,200000);EXPECT_EQ(engine.post(q,0).error,Error::Expired);clock.valid=false;q=show(e,0,TimeKind::Rtp,0);EXPECT_EQ(engine.post(q,0).error,Error::Expired);clock.valid=true;clock.uncertainty_us=50001;q=show(e,0,TimeKind::Rtp,0);EXPECT_EQ(engine.post(q,0).error,Error::Expired);EXPECT_EQ(engine.accepted_revision,0u);}
TEST_F(Fixture,ExpiryAndRestartReleaseSchedulePins){auto* e=load();auto q=show(e,0,TimeKind::Rtp,48000);engine.post(q,0);engine.tick(2070001);EXPECT_EQ(engine.result(q.meta.id)->state,ResultState::Expired);EXPECT_EQ(e->references(),0u);q=show(e,0,TimeKind::Rtp,48000);engine.post(q,0);clock.restart();engine.tick(0);EXPECT_EQ(engine.result(q.meta.id)->state,ResultState::Expired);EXPECT_FALSE(engine.scene.layers[0].visible);}
TEST(Clock,WrapPlcInsertionDropTrim){FakeClock c;c.count=5;c.spans[0]={0xffffff00u,512,1000000,FakeClock::Kind::Plc};c.spans[1]={256,960,1030666,FakeClock::Kind::Audio};c.spans[2]={256,0,1010666,FakeClock::Kind::Insert};c.spans[3]={1216,960,1050666,FakeClock::Kind::Drop};c.spans[4]={2176,480,1050666,FakeClock::Kind::Trim};EXPECT_EQ(c.map(0,c.epoch).speaker_us,1005333);EXPECT_EQ(c.map(256,c.epoch).speaker_us,1030666);EXPECT_EQ(c.map(1500,c.epoch).state,MappingState::Skipped);EXPECT_EQ(c.map(2200,c.epoch).state,MappingState::Skipped);EXPECT_EQ(c.map(2656,c.epoch).state,MappingState::Unknown);EXPECT_EQ(c.map(0,c.epoch+1).state,MappingState::Unknown);}
TEST(Scene,ClipExtremeSignedCoordinatesAndHalfOpenAlignment){auto r=clip_align({INT32_MIN,INT32_MIN,INT32_MAX,INT32_MAX});EXPECT_EQ(r.x0,0);EXPECT_EQ(r.x1,PanelWidth);EXPECT_TRUE(clip_align({100,100,90,90}).empty());r=clip_align({1,3,5,7});EXPECT_EQ(r.x0,PanelXAlign==1?1:0);EXPECT_EQ(r.x1,PanelXAlign==1?5:8);EXPECT_EQ(r.y0,3);EXPECT_EQ(r.y1,7);}
TEST(Scene,DirtyOverflowCoversEveryRegion){Dirty d;for(int i=0;i<9;++i)d.add({i*40,i*40,i*40+4,i*40+4});EXPECT_EQ(d.count,1u);EXPECT_EQ(d.rectangles[0].x1,324);}
TEST(Blend,FractionalIndependentReference){for(unsigned a=0;a<256;++a)for(unsigned k=0;k<16;++k){uint16_t s=uint16_t(k*4099),d=uint16_t(k*997+12345);auto rgb=[](uint16_t v){unsigned r=v>>11,g=(v>>5)&63,b=v&31;return std::array<unsigned,3>{(r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)};};auto source=rgb(s),dest=rgb(d);unsigned out[3];for(unsigned i=0;i<3;++i)out[i]=(source[i]*a+dest[i]*(255-a)+127)/255;uint16_t expected=uint16_t(((out[0]*31+127)/255)<<11|((out[1]*63+127)/255)<<5|((out[2]*31+127)/255));if(a==0)expected=d;if(a==255)expected=s;EXPECT_EQ(blend(d,{s,uint8_t(a)}),expected);}}
TEST(Hash,RejectPathTraversalUppercaseAndWrongLength){EXPECT_FALSE(valid_hash("../thing"));EXPECT_FALSE(valid_hash(""));std::string h(64,'a');EXPECT_TRUE(valid_hash(h.c_str()));h[32]='A';EXPECT_FALSE(valid_hash(h.c_str()));EXPECT_FALSE(valid_hash(std::string(65,'0').c_str()));}
struct Files:testing::Test {std::string root;std::vector<uint8_t> scratch=std::vector<uint8_t>(524288);FlashStore store;std::vector<uint8_t> body=raw(5,3);char hash[65]{};
    void SetUp()override{std::string name=std::string(TEST_STORE_ROOT)+"/store-XXXXXX";std::vector<char> path(name.begin(),name.end());path.push_back(0);ASSERT_NE(mkdtemp(path.data()),nullptr);root=path.data();digest(body.data(),body.size(),hash);ASSERT_EQ(store.mount(root.c_str(),4*1024*1024,scratch.data(),scratch.size()),Error::None);}
    void TearDown()override{std::filesystem::remove_all(root);}
};
TEST_F(Files,AtomicInstallReadDeleteAndIdempotence){ASSERT_EQ(store.install(hash,body.data(),body.size()),Error::None);EXPECT_EQ(store.install(hash,body.data(),body.size()),Error::None);EXPECT_EQ(store.writes,1u);size_t size=0;ASSERT_EQ(store.read(hash,scratch.data(),scratch.size(),size),Error::None);EXPECT_EQ(size,body.size());EXPECT_EQ(std::memcmp(body.data(),scratch.data(),size),0);ASSERT_EQ(store.erase(hash),Error::None);EXPECT_EQ(store.erase(hash),Error::None);}
TEST_F(Files,EveryWriteSyncCloseRenameFailureKeepsFinalAbsent){for(auto fault:{FlashStore::Fault::Write,FlashStore::Fault::Sync,FlashStore::Fault::Close,FlashStore::Fault::Rename}){store.fault=fault;EXPECT_NE(store.install(hash,body.data(),body.size()),Error::None);EXPECT_FALSE(std::filesystem::exists(root+"/"+hash));EXPECT_EQ(store.count,0u);EXPECT_EQ(store.temp_bytes,0u);}}
TEST_F(Files,InterruptedTempCleanupAndValidatedCatalog){std::ofstream(root+"/"+hash+".tmp",std::ios::binary).write("broken",6);ASSERT_EQ(store.mount(root.c_str(),4*1024*1024,scratch.data(),scratch.size()),Error::None);EXPECT_FALSE(std::filesystem::exists(root+"/"+hash+".tmp"));store.install(hash,body.data(),body.size());ASSERT_EQ(store.mount(root.c_str(),4*1024*1024,scratch.data(),scratch.size()),Error::None);EXPECT_EQ(store.count,1u);EXPECT_TRUE(store.find(hash)->valid);}
TEST_F(Files,CorruptFinalExcludedButNotDeleted){std::ofstream(root+"/"+hash,std::ios::binary).write("broken",6);ASSERT_EQ(store.mount(root.c_str(),4*1024*1024,scratch.data(),scratch.size()),Error::None);ASSERT_NE(store.find(hash),nullptr);EXPECT_FALSE(store.find(hash)->valid);EXPECT_EQ(store.find(hash)->error,Error::Hash);EXPECT_TRUE(std::filesystem::exists(root+"/"+hash));}
TEST_F(Files,EnospcNeverEvictsFinal){ASSERT_EQ(store.install(hash,body.data(),body.size()),Error::None);ASSERT_EQ(store.mount(root.c_str(),body.size()+512,scratch.data(),scratch.size()),Error::None);auto b=raw(1,1,0x001f);char another[65];digest(b.data(),b.size(),another);EXPECT_EQ(store.install(another,b.data(),b.size()),Error::FlashFull);EXPECT_TRUE(std::filesystem::exists(root+"/"+hash));}
TEST_F(Files,MmapGuardAndHashValidationBeforePathUse){store.write_allowed=[](){return false;};EXPECT_EQ(store.install(hash,body.data(),body.size()),Error::StorageUnavailable);store.write_allowed=nullptr;EXPECT_EQ(store.install("../escape",body.data(),body.size()),Error::Malformed);auto b=body;b.back()^=1;EXPECT_EQ(store.install(hash,b.data(),b.size()),Error::Hash);EXPECT_EQ(store.count,0u);}
}
#include "dg_display/json_request.h"
namespace {
TEST(Parser,ConsoleDefaultsAndWireRequireMetadata){Request q;Meta defaults;defaults.epoch=5;defaults.sequence=7;std::strcpy(defaults.id,"console-1");const char* json="{\"color\":65535}";EXPECT_EQ(parse_request("fill",json,std::strlen(json),&defaults,q),Error::None);EXPECT_EQ(q.meta.epoch,5u);EXPECT_EQ(q.meta.sequence,7u);EXPECT_EQ(q.operations[0].color,65535u);EXPECT_EQ(parse_request("fill",json,std::strlen(json),nullptr,q),Error::Malformed);}
TEST(Parser,ExplicitMetadataKeepsSharedValidation){const char* json="{\"v\":1,\"epoch\":2,\"id\":\"wire-1\",\"seq\":4,\"ops\":[{\"op\":\"fill\",\"color\":0},{\"op\":\"hide\",\"layer\":7}],\"at\":{\"kind\":\"rtp\",\"timestamp\":4294967295,\"clock_epoch\":3,\"utterance_id\":\"u\"}}";Request q;ASSERT_EQ(parse_request("batch",json,std::strlen(json),nullptr,q),Error::None);EXPECT_EQ(q.timestamp,UINT32_MAX);EXPECT_EQ(q.count,2u);EXPECT_EQ(q.time,TimeKind::Rtp);EXPECT_STREQ(q.utterance,"u");}
TEST(Parser,BatchRejectsNestedIoAndTooManyOps){Meta m;m.epoch=1;m.sequence=1;std::strcpy(m.id,"a");Request q;std::string j="{\"ops\":[{\"op\":\"load\"}]}";EXPECT_EQ(parse_request("batch",j.data(),j.size(),&m,q),Error::Malformed);j="{\"ops\":[";for(unsigned i=0;i<9;++i)j+=(i?",":"")+std::string("{\"op\":\"hide\"}");j+="]}";EXPECT_EQ(parse_request("batch",j.data(),j.size(),&m,q),Error::Malformed);}
TEST(Parser,RejectsFractionalOverflowWrongTypesEmbeddedNulAndOversize){Meta m;m.epoch=1;m.sequence=1;std::strcpy(m.id,"a");Request q;for(const char* j:{"{\"color\":0.5}","{\"color\":65536}","{\"seq\":4294967296,\"color\":0}","{\"seq\":-1,\"color\":0}","{\"id\":\"a\\u0000b\",\"color\":0}","{\"color\":\"1\"}"})EXPECT_EQ(parse_request("fill",j,std::strlen(j),&m,q),Error::Malformed);std::string big(4097,' ');EXPECT_EQ(parse_request("fill",big.data(),big.size(),&m,q),Error::Malformed);EXPECT_EQ(parse_request("fill","[[[[[[[[[0]]]]]]]]]",19,&m,q),Error::Malformed);}
TEST(Parser,MalformedAndTruncatedJsonLeavesTypedOutputUntouched){Meta m;m.epoch=1;m.sequence=1;std::strcpy(m.id,"a");Request q;q.bytes=123;const char* j="{\"color\":32}";for(size_t n=1;n<std::strlen(j);++n){EXPECT_EQ(parse_request("fill",j,n,&m,q),Error::Malformed);EXPECT_EQ(q.bytes,123u);}}
}
namespace {
TEST(Scene,DmaExpansionUsesEvenYOnlyForEightByteRows){auto r=dma_align({1,3,5,7});EXPECT_EQ(r.x0,0);EXPECT_EQ(r.x1,8);EXPECT_EQ(r.y0,3);EXPECT_EQ(r.y1,7);r=dma_align({0,3,4,7});EXPECT_EQ(r.y0,DmaXAlign==8?3:2);EXPECT_EQ(r.y1,DmaXAlign==8?7:8);EXPECT_EQ((r.x1-r.x0)*(r.y1-r.y0)*2%16,0);r=dma_align({PanelWidth-4,PanelHeight-1,PanelWidth,PanelHeight});EXPECT_EQ(r.y0,DmaXAlign==8?PanelHeight-1:PanelHeight-2);EXPECT_EQ(r.y1,PanelHeight);}
}
namespace {
TEST_F(Fixture,OffscreenCommitCompletesWithoutDma){auto q=show(load());q.operations[0].x=-32768;q.operations[0].y=32767;ASSERT_EQ(engine.post(q,0).admission,Admission::Accepted);engine.tick(0);EXPECT_EQ(engine.result(q.meta.id)->state,ResultState::Completed);EXPECT_EQ(engine.completed_revision,1u);EXPECT_EQ(engine.pixels,0u);}
TEST_F(Fixture,FutureTextHandleOwnsItsOwnReferences){struct Text {unsigned rendering=0,visible=1;} text;ImageHandle handle;handle.context=&text;handle.width=1;handle.height=1;handle.reference=&text;handle.retain=[](void* p,Pin kind){if(kind==Pin::Rendering)++static_cast<Text*>(p)->rendering;return true;};handle.release=[](void* p,Pin kind){if(kind==Pin::Rendering)--static_cast<Text*>(p)->rendering;if(kind==Pin::Visible)--static_cast<Text*>(p)->visible;};engine.scene.layers[0]={&handle,0,0,true};engine.dirty.add({0,0,4,1});RenderLease lease;ASSERT_TRUE(engine.snapshot(lease));EXPECT_EQ(text.rendering,1u);engine.transport_down();EXPECT_EQ(text.visible,0u);engine.complete(lease,false,0);EXPECT_EQ(text.rendering,0u);}
TEST(Parser,RejectsExcessiveStructuralCountBeforeParsing){std::string json="{\"unused\":[";for(unsigned i=0;i<128;++i)json+=(i?",":"")+std::string("{}");json+="],\"color\":0}";Meta m;m.epoch=1;m.sequence=1;std::strcpy(m.id,"a");Request q;EXPECT_EQ(parse_request("fill",json.data(),json.size(),&m,q),Error::Malformed);}
}
namespace {
TEST_F(Fixture,SnapshotAcquiresWholeReferenceSetOrLeavesDirty){unsigned references=0;ImageHandle a,b;a.width=a.height=b.width=b.height=1;a.reference=&references;a.retain=[](void* p,Pin){++*static_cast<unsigned*>(p);return true;};a.release=[](void* p,Pin){--*static_cast<unsigned*>(p);};b.retain=[](void*,Pin){return false;};engine.scene.layers[0]={&a,0,0,true};engine.scene.layers[1]={&b,0,0,true};engine.dirty.add({0,0,4,1});RenderLease lease;EXPECT_FALSE(engine.snapshot(lease));EXPECT_EQ(references,0u);EXPECT_FALSE(engine.rendering);EXPECT_EQ(engine.dirty.count,1u);}
}
#include <sys/wait.h>
#include <unistd.h>
namespace {
unsigned crash_stage=0;
void interrupted(unsigned stage,size_t){if(stage==crash_stage)_exit(99);}
TEST_F(Files,ProcessInterruptionBeforeCompleteBeforeRenameAndAfterRename){
    auto b=raw(PanelWidth,PanelHeight,0x1234);char identity[65];digest(b.data(),b.size(),identity);
    for(unsigned stage=1;stage<=3;++stage){crash_stage=stage;pid_t child=fork();ASSERT_GE(child,0);if(child==0){store.checkpoint=interrupted;store.install(identity,b.data(),b.size());_exit(1);}int status=0;ASSERT_EQ(waitpid(child,&status,0),child);ASSERT_TRUE(WIFEXITED(status));ASSERT_EQ(WEXITSTATUS(status),99);ASSERT_EQ(store.mount(root.c_str(),4*1024*1024,scratch.data(),scratch.size()),Error::None);auto* item=store.find(identity);if(stage<3)EXPECT_EQ(item,nullptr);else{ASSERT_NE(item,nullptr);EXPECT_TRUE(item->valid);store.erase(identity);}}
}
bool permitted=true;
void deny_after_chunk(unsigned stage,size_t){if(stage==1)permitted=false;}
TEST_F(Files,SessionFenceLeavesTempForLaterCleanup){auto b=raw(128,100,0x4567);char identity[65];digest(b.data(),b.size(),identity);permitted=true;store.write_allowed=[](){return permitted;};store.checkpoint=deny_after_chunk;EXPECT_EQ(store.install(identity,b.data(),b.size()),Error::StorageUnavailable);EXPECT_EQ(store.temp_bytes,4096u);EXPECT_FALSE(std::filesystem::exists(root+"/"+identity));EXPECT_TRUE(std::filesystem::exists(root+"/"+identity+".tmp"));permitted=true;store.checkpoint=nullptr;EXPECT_EQ(store.mount(root.c_str(),4*1024*1024,scratch.data(),scratch.size()),Error::None);EXPECT_FALSE(std::filesystem::exists(root+"/"+identity+".tmp"));}
TEST_F(Files,ConservativeFilesystemCapacityRejectsBeforeWrite){store.free_bytes=[](){return size_t(16000);};EXPECT_EQ(store.max_admissible(),0u);EXPECT_EQ(store.install(hash,body.data(),body.size()),Error::FlashFull);EXPECT_EQ(store.writes,0u);EXPECT_FALSE(std::filesystem::exists(root+"/"+hash+".tmp"));}
}

TEST(Scene,EveryEdgeDmaRegionIsBoundedAndSixteenByteSized){
    for(int x=-2;x<PanelWidth+2;++x)for(int y=-2;y<PanelHeight+2;++y){
        auto r=dma_align({x,y,x+1,y+1});if(r.empty())continue;
        ASSERT_GE(r.x0,0);ASSERT_GE(r.y0,0);ASSERT_LE(r.x1,PanelWidth);ASSERT_LE(r.y1,PanelHeight);
        ASSERT_EQ((r.x1-r.x0)*(r.y1-r.y0)*2%16,0);
    }
}

TEST(Parser,ServerDisplayEnvelopePreservesWireMetadataAndRejectsOriginBypass){
    Request q;bool matched=false;
    const char* wire=R"({"t":"display.fill","d":{"v":1,"epoch":42,"seq":7,"id":"wire-7","color":4660}})";
    ASSERT_EQ(parse_server_request(wire,std::strlen(wire),q,matched),Error::None);ASSERT_TRUE(matched);
    EXPECT_EQ(q.meta.epoch,42u);EXPECT_EQ(q.meta.sequence,7u);EXPECT_EQ(q.operations[0].color,4660);
    const char* bypass=R"({"t":"display.load","d":{"any_host":true}})";
    EXPECT_EQ(parse_server_request(bypass,std::strlen(bypass),q,matched),Error::Malformed);EXPECT_TRUE(matched);
    const char* other=R"({"t":"turn-transcript","d":{"text":"hello"}})";
    EXPECT_EQ(parse_server_request(other,std::strlen(other),q,matched),Error::Malformed);EXPECT_FALSE(matched);
    auto oversized=std::string(1024,' ');EXPECT_EQ(parse_server_request(oversized.data(),oversized.size(),q,matched),Error::Malformed);
}

TEST_F(Fixture,InternalAnimationDoesNotConsumeBotSequence) {
    auto* entry=load();
    auto wire=show(entry);wire.meta.sequence=1;std::strcpy(wire.meta.id,"bot-1");
    ASSERT_EQ(engine.post(wire,0).error,Error::None);
    auto animation=show(entry);animation.meta.sequence=100;std::strcpy(animation.meta.id,"bench-100");
    ASSERT_EQ(engine.post(animation,1,true).error,Error::None);
    EXPECT_EQ(engine.last_sequence,1u);
    wire.meta.sequence=2;std::strcpy(wire.meta.id,"bot-2");
    EXPECT_EQ(engine.post(wire,2).error,Error::None);
    wire.meta.sequence=1;std::strcpy(wire.meta.id,"stale-bot");
    EXPECT_EQ(engine.post(wire,3).error,Error::StaleSequence);
    animation.meta.epoch++;
    EXPECT_EQ(engine.post(animation,4,true).error,Error::StaleEpoch);
}

TEST(Parser,MalformedWireRetainsValidatedCorrelationWithoutChangingRequest){
    const char* json="{\"t\":\"display.show\",\"d\":{\"v\":1,\"epoch\":42,\"seq\":7,\"id\":\"bad-show\",\"layer\":99}}";
    Request q;q.bytes=123;Meta correlation;bool matched=false;
    EXPECT_EQ(parse_server_request(json,std::strlen(json),q,matched,&correlation),Error::Malformed);
    EXPECT_TRUE(matched);EXPECT_EQ(q.bytes,123u);EXPECT_EQ(correlation.epoch,42u);
    EXPECT_EQ(correlation.sequence,7u);EXPECT_STREQ(correlation.id,"bad-show");
}
TEST(Parser,MalformedWireDoesNotCoerceInvalidCorrelation){
    const char* json="{\"t\":\"display.show\",\"d\":{\"epoch\":-1,\"seq\":true,\"id\":42}}";
    Request q;Meta correlation;bool matched=false;
    EXPECT_EQ(parse_server_request(json,std::strlen(json),q,matched,&correlation),Error::Malformed);
    EXPECT_TRUE(matched);EXPECT_EQ(correlation.epoch,0u);EXPECT_EQ(correlation.sequence,0u);EXPECT_STREQ(correlation.id,"");
}

#include "dg_display/dma_watchdog.h"
TEST(DmaWatchdog, StarvedOwnerBeforeQueueOrAfterIsrIsNotHardwareLoss) {
    EXPECT_FALSE(dg_display::missing_dma_completion(9000000,1000,900,0,true));
    EXPECT_FALSE(dg_display::missing_dma_completion(9000000,1000,1100,1200,false));
    EXPECT_FALSE(dg_display::missing_dma_completion(9000000,1000,1100,1200,true));
    EXPECT_FALSE(dg_display::missing_dma_completion(1500,1000,1100,0,true));
    EXPECT_FALSE(dg_display::missing_dma_completion(9000000,1000,8999000,0,true));
    EXPECT_TRUE(dg_display::missing_dma_completion(9000000,1000,1100,0,true));
}
