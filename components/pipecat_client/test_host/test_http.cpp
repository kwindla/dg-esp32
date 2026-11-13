#include <gtest/gtest.h>
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "pipecat/esp/http.h"
#include "pipecat/errors.h"
#include <cstring>
#include <algorithm>
struct FakeHttp {
    std::string body = "{}", sent;
    std::map<std::string, std::string> headers;
    int status = 200, open_length = -1, writes = 0, reads = 0, cleaned = 0;
    int partial = 3, timeout = 0, delay_ms = 0, header_retries = 0;
    int open_error = 0, write_error = 0, read_error = 0;
    int init_timeout=0, open_timeout=0, open_delay_ms=0, header_delay_ms=0, open_bound=5000;
    std::vector<int> io_timeouts;
    bool chunked = false, complete = true, init_fail = false, reset_status = false;
    esp_err_t (*bundle)(void*) = nullptr;
    size_t offset = 0;
    int64_t now = 0;
} f;
int64_t esp_timer_get_time() { return f.now; }
esp_err_t esp_crt_bundle_attach(void*) { return ESP_OK; }
esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t* c) {
    EXPECT_EQ(c->method, HTTP_METHOD_POST); EXPECT_TRUE(c->disable_auto_redirect);
    f.init_timeout=f.timeout = c->timeout_ms; f.bundle = c->crt_bundle_attach; return f.init_fail ? nullptr : &f;
}
int esp_http_client_cleanup(FakeHttp*) { ++f.cleaned; return 0; }
int esp_http_client_set_timeout_ms(FakeHttp*, int ms) { EXPECT_GT(ms, 0); EXPECT_LE(ms, f.open_bound); f.timeout = ms; return 0; }
int esp_http_client_set_header(FakeHttp*, const char* k, const char* v) { f.headers[k] = v; return 0; }
int esp_http_client_open(FakeHttp*, int n) { f.open_length = n;f.open_timeout=f.timeout;f.now+=f.open_delay_ms*1000LL;return f.open_error; }
int esp_http_client_write(FakeHttp*, const char* data, int n) {
    f.io_timeouts.push_back(f.timeout);++f.writes; if (f.write_error) return f.write_error;
    n = std::min(n, f.partial); f.sent.append(data,n); return n;
}
int64_t esp_http_client_fetch_headers(FakeHttp*) {
    f.io_timeouts.push_back(f.timeout);f.now+=f.header_delay_ms*1000LL;
    if (f.header_retries-- > 0) { f.now += 100000; return -ESP_ERR_HTTP_EAGAIN; }
    if (f.reset_status) f.status = -1;
    return f.chunked ? 0 : f.body.size();
}
int esp_http_client_get_status_code(FakeHttp*) { return f.status; }
int esp_http_client_read(FakeHttp*, char* data, int n) {
    f.io_timeouts.push_back(f.timeout);++f.reads; f.now += f.delay_ms * 1000LL;
    if (f.read_error) return f.read_error;
    n = std::min<size_t>(n, f.body.size()-f.offset);
    memcpy(data, f.body.data()+f.offset, n); f.offset += n; return n;
}
bool esp_http_client_is_complete_data_received(FakeHttp*) { return f.complete; }
class Http : public testing::Test {
protected:
    pipecat::APIRequest req;
    void SetUp() override { f = {}; req.endpoint = "http://test/start"; }
    pipecat::esp::HttpResponse post() { return pipecat::esp::http_post(req, [] { return false; }); }
};
TEST_F(Http, NullBody) { EXPECT_EQ(post().body,"{}"); EXPECT_EQ(f.open_length,0); EXPECT_EQ(f.writes,0); EXPECT_EQ(f.headers["Content-Type"],"application/json"); EXPECT_EQ(f.cleaned,1); }
TEST_F(Http, PartialWritesAndHeaders) { req.request_data={{"transport","webrtc"}}; req.headers={{"content-type","custom"},{"Authorization","test"}}; post(); EXPECT_EQ(f.sent,req.request_data.dump()); EXPECT_GT(f.writes,1); EXPECT_EQ(f.headers.count("Content-Type"),0u); EXPECT_EQ(f.headers["Authorization"],"test"); }
TEST_F(Http, Chunked) { f.chunked=true; f.body=std::string(3500,'a'); EXPECT_EQ(post().body,f.body); }
TEST_F(Http, ExactLimit) { f.body=std::string(65536,'a'); EXPECT_EQ(post().body.size(),65536u); }
TEST_F(Http, OversizeLength) { f.body=std::string(65537,'a'); EXPECT_THROW(post(),pipecat::StartBotError); EXPECT_EQ(f.reads,0); EXPECT_EQ(f.cleaned,1); }
TEST_F(Http, OversizeChunked) { f.chunked=true; f.body=std::string(65537,'a'); EXPECT_THROW(post(),pipecat::StartBotError); EXPECT_EQ(f.cleaned,1); }
TEST_F(Http, NonSuccessReturned) { f.status=503; EXPECT_EQ(post().status,503); }
TEST_F(Http, CancelMidRead) { f.body=std::string(3000,'a'); try { pipecat::esp::http_post(req,[]{return f.reads>0;}); FAIL(); } catch (const pipecat::PipecatError& e) { EXPECT_STREQ(e.what(),"start_bot() was cancelled by disconnect()"); } EXPECT_EQ(f.cleaned,1); EXPECT_EQ(f.reads,1); }
TEST_F(Http, Deadline) { req.timeout=std::chrono::milliseconds(10); f.delay_ms=11; EXPECT_THROW(post(),pipecat::StartBotError); EXPECT_EQ(f.timeout,10); EXPECT_EQ(f.cleaned,1); }
TEST_F(Http, ZeroTimeoutIsThirtySeconds) { f.read_error=-ESP_ERR_HTTP_EAGAIN; f.delay_ms=500; EXPECT_THROW(post(),pipecat::StartBotError); EXPECT_EQ(f.now,30000000); }
TEST_F(Http, Incomplete) { f.complete=false; EXPECT_THROW(post(),pipecat::StartBotError); EXPECT_EQ(f.cleaned,1); }
TEST_F(Http, HeaderRetry) { f.header_retries=2; EXPECT_EQ(post().status,200); }
TEST_F(Http, StatusSurvivesHeaderRetry) { f.header_retries=1; f.reset_status=true; EXPECT_EQ(post().status,200); }
TEST_F(Http, OpenFailure) { f.open_error=-1; EXPECT_THROW(post(),pipecat::StartBotError); EXPECT_EQ(f.cleaned,1); }
TEST_F(Http, WriteFailure) { req.request_data="a"; f.write_error=-1; EXPECT_THROW(post(),pipecat::StartBotError); EXPECT_EQ(f.cleaned,1); }
TEST_F(Http, ReadFailure) { f.read_error=-1; EXPECT_THROW(post(),pipecat::StartBotError); EXPECT_EQ(f.cleaned,1); }
TEST_F(Http, InitFailure) { f.init_fail=true; EXPECT_THROW(post(),pipecat::StartBotError); }

TEST_F(Http, OpenUsesConnectBudgetThenIoUsesSlices) {
    req.request_data={{"body",1}};f.open_delay_ms=900;post();
    EXPECT_EQ(f.init_timeout,5000);EXPECT_EQ(f.open_timeout,5000);
    ASSERT_FALSE(f.io_timeouts.empty());
    for(auto timeout:f.io_timeouts)EXPECT_LE(timeout,500);
    EXPECT_EQ(f.cleaned,1);
}
TEST_F(Http, SharedDeadlineClipsOpenAndSubsequentIo) {
    req.timeout=std::chrono::milliseconds(1000);f.open_delay_ms=750;post();
    EXPECT_EQ(f.init_timeout,1000);EXPECT_EQ(f.open_timeout,1000);
    for(auto timeout:f.io_timeouts)EXPECT_EQ(timeout,250);
}
TEST_F(Http, CancelDuringSynchronousOpenCleansWithoutSendingBody) {
    req.request_data={{"body",1}};f.open_delay_ms=4000;
    EXPECT_THROW(pipecat::esp::http_post(req,[]{return f.now>=4000000;}),pipecat::PipecatError);
    EXPECT_EQ(f.open_timeout,5000);EXPECT_EQ(f.writes,0);EXPECT_EQ(f.cleaned,1);
}
TEST_F(Http, DeadlineAfterOpenAndHeadersIsChecked) {
    req.timeout=std::chrono::milliseconds(900);f.open_delay_ms=901;
    EXPECT_THROW(post(),pipecat::StartBotError);EXPECT_EQ(f.writes,0);EXPECT_EQ(f.cleaned,1);
    f={};req.timeout=std::chrono::milliseconds(900);f.header_delay_ms=901;
    EXPECT_THROW(post(),pipecat::StartBotError);EXPECT_EQ(f.reads,0);EXPECT_EQ(f.cleaned,1);
}
TEST_F(Http, FailedPostIsNotReplayed) {
    req.request_data={{"body",1}};f.read_error=-1;
    EXPECT_THROW(post(),pipecat::StartBotError);
    EXPECT_EQ(f.sent,req.request_data.dump());EXPECT_EQ(f.cleaned,1);
}
TEST_F(Http, HttpsOpensWithFifteenSecondsAndTheCertificateBundle) {
    req.endpoint="https://test/start";f.open_bound=15000;post();
    EXPECT_EQ(f.init_timeout,15000);EXPECT_EQ(f.open_timeout,15000);EXPECT_EQ(f.bundle,&esp_crt_bundle_attach);
    for(auto timeout:f.io_timeouts)EXPECT_LE(timeout,500);
    EXPECT_EQ(f.cleaned,1);
}
TEST_F(Http, HttpKeepsTheLanOpenBudgetWithoutABundle) {
    post();EXPECT_EQ(f.init_timeout,5000);EXPECT_EQ(f.bundle,nullptr);
}
TEST_F(Http, HttpsOpenBudgetStillClipsToTheSharedDeadline) {
    req.endpoint="https://test/start";req.timeout=std::chrono::milliseconds(1000);post();
    EXPECT_EQ(f.init_timeout,1000);
}
TEST_F(Http, ErrorTextNamesTheOriginNotThePath) {
    req.endpoint="https://demo.example:8443/s3cr3t-path/start";f.open_bound=15000;f.open_error=-1;
    try { post(); FAIL(); } catch (const pipecat::StartBotError& e) {
        const std::string what=e.what();
        EXPECT_EQ(what.find("s3cr3t"),std::string::npos);
        EXPECT_NE(what.find("Unable to reach https://demo.example:8443: "),std::string::npos);
    }
    EXPECT_EQ(f.cleaned,1);
}
TEST_F(Http, ErrorTextNamesOnlyAWellFormedSchemeHostAndPort) {
    struct Case { const char* endpoint; const char* origin; } cases[] = {
        {"https://user:s3cr3t@demo.example:8443/path/start", "https://demo.example:8443"},
        {"http://s3cr3t@demo.example/start", "http://demo.example"},
        {"http://demo.example?token=s3cr3t", "http://demo.example"},
        {"http://demo.example#s3cr3t", "http://demo.example"},
        {"http://user:s3cr3t@demo.example?token=s3cr3t#frag", "http://demo.example"},
        {"https://[2001:db8::7]:8443/s3cr3t/start", "https://[2001:db8::7]:8443"},
        {"https://user:s3cr3t@[::1]/start", "https://[::1]"},
        {"http://192.0.2.7:7861/s3cr3t", "http://192.0.2.7:7861"},
        {"http://bench_host.local:7861/s3cr3t", "http://bench_host.local:7861"},
        // Not well formed: nothing of the endpoint is echoed.
        {"demo.example/s3cr3t", "the configured endpoint"},
        {"s3cr3t", "the configured endpoint"},
        {"://s3cr3t.example/start", "the configured endpoint"},
        {"ht!tp://s3cr3t.example/start", "the configured endpoint"},
        {"https://", "the configured endpoint"},
        {"https://user:s3cr3t/x@demo.example/start", "the configured endpoint"},
        {"https://demo.example:s3cr3t/start", "the configured endpoint"},
        {"https://demo.example:/s3cr3t", "the configured endpoint"},
        {"https://s3cr3t example/start", "the configured endpoint"},
        {"https://[s3cr3t]/start", "the configured endpoint"},
        {"https://[::1/s3cr3t", "the configured endpoint"},
        {"https://[]/s3cr3t", "the configured endpoint"},
    };
    for (const auto& c : cases) {
        f = {}; f.open_bound=15000; f.open_error=-1; req.endpoint=c.endpoint;
        try { post(); FAIL() << c.endpoint; } catch (const pipecat::StartBotError& e) {
            const std::string what=e.what();
            EXPECT_EQ(what.find("s3cr3t"),std::string::npos) << what;
            EXPECT_NE(what.find(std::string("Unable to reach ")+c.origin+": "),std::string::npos) << what;
        }
        EXPECT_EQ(f.cleaned,1) << c.endpoint;
    }
}
