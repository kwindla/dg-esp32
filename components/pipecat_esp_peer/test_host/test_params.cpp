//
// Copyright (c) 2024-2026, Daily
//
// SPDX-License-Identifier: BSD-2-Clause
//

#include "params.h"

#include <pipecat/errors.h>

#include <gtest/gtest.h>

using namespace pipecat;
using namespace pipecat::esp_peer;
using nlohmann::json;

namespace {

APIRequest start_request(const std::string& endpoint) {
    APIRequest request;
    request.endpoint = endpoint;
    request.headers = {{"Authorization", "Bearer secret"}};
    request.request_data = {{"transport", "webrtc"}};
    return request;
}

}  // namespace

TEST(SessionOfferUrl, ReplacesStart) {
    EXPECT_EQ(
            session_offer_url("http://localhost:7861/start", "1234"),
            "http://localhost:7861/sessions/1234/api/offer"
    );
    EXPECT_EQ(
            session_offer_url(
                    "https://api.pipecat.daily.co/v1/public/agent/start", "1234"
            ),
            "https://api.pipecat.daily.co/v1/public/agent/sessions/1234/api/"
            "offer"
    );
}

TEST(SessionOfferUrl, KeepsTheQuery) {
    EXPECT_EQ(
            session_offer_url("http://localhost:7861/start?a=1#b", "1234"),
            "http://localhost:7861/sessions/1234/api/offer?a=1#b"
    );
}

TEST(SessionOfferUrl, OnlyLooksAtThePath) {
    EXPECT_EQ(
            session_offer_url("https://start.example.com/start", "1234"),
            "https://start.example.com/sessions/1234/api/offer"
    );
    EXPECT_THROW(
            session_offer_url("http://start", "1234"),
            InvalidTransportParamsError
    );
    EXPECT_THROW(
            session_offer_url("http://example.com/restart", "1234"),
            InvalidTransportParamsError
    );
    EXPECT_THROW(
            session_offer_url("http://example.com/start/now", "1234"),
            InvalidTransportParamsError
    );
}

TEST(ConnectionParams, SessionFromStartBot) {
    auto params = parse_connection_params(
            {{"sessionId", "1234"}},
            start_request("http://localhost:7861/start")
    );
    EXPECT_EQ(
            params.offer_url, "http://localhost:7861/sessions/1234/api/offer"
    );
    EXPECT_EQ(params.headers.at("Authorization"), "Bearer secret");
    // The start endpoint already has the bot's data.
    EXPECT_TRUE(params.request_data.is_null());
    EXPECT_TRUE(params.ice_servers.empty());
}

TEST(ConnectionParams, SessionNeedsStartBot) {
    EXPECT_THROW(
            parse_connection_params({{"sessionId", "1234"}}, std::nullopt),
            InvalidTransportParamsError
    );
}

TEST(ConnectionParams, RequestParams) {
    json params = {
            {"webrtcRequestParams",
             {{"endpoint", "http://localhost:7861/api/offer"},
              {"headers", {{"Authorization", "Bearer other"}}},
              {"requestData", {{"voice", "calm"}}}}},
            // Ignored, since the request params win.
            {"sessionId", "1234"},
    };
    auto result = parse_connection_params(
            params, start_request("http://localhost:7861/start")
    );
    EXPECT_EQ(result.offer_url, "http://localhost:7861/api/offer");
    EXPECT_EQ(result.headers.at("Authorization"), "Bearer other");
    EXPECT_EQ(result.request_data, json({{"voice", "calm"}}));
}

TEST(ConnectionParams, SnakeCase) {
    json params = {
            {"webrtc_request_params",
             {{"endpoint", "http://localhost:7861/api/offer"},
              {"request_data", {{"voice", "calm"}}}}},
            {"ice_config",
             {{"ice_servers", {{{"urls", "stun:stun.l.google.com:19302"}}}}}},
    };
    auto result = parse_connection_params(params, std::nullopt);
    EXPECT_EQ(result.offer_url, "http://localhost:7861/api/offer");
    EXPECT_EQ(result.request_data, json({{"voice", "calm"}}));
    ASSERT_EQ(result.ice_servers.size(), 1u);

    result = parse_connection_params(
            {{"session_id", "1234"}},
            start_request("http://localhost:7861/start")
    );
    EXPECT_EQ(
            result.offer_url, "http://localhost:7861/sessions/1234/api/offer"
    );
}

TEST(ConnectionParams, Invalid) {
    auto start = start_request("http://localhost:7861/start");
    for (const char* params: {
                 R"(null)",
                 R"("http://localhost:7861/api/offer")",
                 R"({})",
                 R"({"sessionId": 1234})",
                 R"({"webrtcRequestParams": "http://localhost/api/offer"})",
                 R"({"webrtcRequestParams": {}})",
                 R"({"webrtcRequestParams": {"endpoint": "http://localhost/api/offer", "headers": {"Authorization": 1}}})",
                 R"({"sessionId": "1234", "iceConfig": "stun:x"})",
                 R"({"sessionId": "1234", "iceConfig": {"iceServers": [{"urls": 1}]}})",
                 R"({"sessionId": "1234", "iceConfig": {"iceServers": [{"urls": "http://x"}]}})",
         }) {
        EXPECT_THROW(
                parse_connection_params(json::parse(params), start),
                InvalidTransportParamsError
        ) << params;
    }
}

TEST(ConnectionParams, StunListsAndTurnRejection) {
    auto start = start_request("http://localhost:7861/start");
    json p = {{"sessionId", "id"}, {"iceConfig", {{"iceServers", json::array({
        {{"urls", json::array({"stun:example.com:3478", "stun:[::1]:3478"})}},
        {{"urls", "stun:other"}, {"username", "u"}, {"credential", "p"}}
    })}}}};
    auto r = parse_connection_params(p, start);
    ASSERT_EQ(r.ice_servers.size(), 3u);
    EXPECT_EQ(r.ice_servers[0].url, "stun:example.com:3478");
    EXPECT_EQ(r.ice_servers[2].username, "u");
    EXPECT_EQ(r.ice_servers[2].password, "p");
    for (const char* url : {"turn:x", "turns:x"}) {
        p["iceConfig"]["iceServers"][0]["urls"] = url;
        try { parse_connection_params(p, start); FAIL(); }
        catch (const InvalidTransportParamsError& e) {
            EXPECT_STREQ(e.what(), "TURN not supported in LAN mode");
        }
    }
}
TEST(ConnectionParams, PublicPathKeepsOneUdpTurn) {
    auto start = start_request("https://bot.example.com:10000/secret/start");
    json p = {{"sessionId", "id"}, {"iceConfig", {{"iceServers", json::array({
        {{"urls", json::array({"stun:stun.cloudflare.com:3478", "turn:turn.cloudflare.com:3478?transport=udp",
                               "turn:turn.cloudflare.com:3478?transport=tcp", "turns:turn.cloudflare.com:5349?transport=tcp",
                               "turn:turn.cloudflare.com:53?transport=udp"})},
         {"username", "u"}, {"credential", "p"}}
    })}}}};
    auto r = parse_connection_params(p, start);
    ASSERT_EQ(r.ice_servers.size(), 2u);
    EXPECT_EQ(r.ice_servers[0].url, "stun:stun.cloudflare.com:3478");
    EXPECT_EQ(r.ice_servers[1].url, "turn:turn.cloudflare.com:3478");
    EXPECT_EQ(r.ice_servers[1].username, "u");
    EXPECT_EQ(r.ice_servers[1].password, "p");
    json bare = {{"sessionId", "id"}, {"iceConfig", {{"iceServers", json::array({
        {{"urls", "turn:turn.cloudflare.com:3478"}}})}}}};
    EXPECT_THROW(parse_connection_params(bare, start), InvalidTransportParamsError);
    // The same list on the LAN path keeps rejecting TURN.
    EXPECT_THROW(parse_connection_params(p, start_request("http://localhost:7861/start")), InvalidTransportParamsError);
}
TEST(ConnectionParams, RejectsMalformedIce) {
    for (const char* url : {"stun:", "stun://host", "stun:host:", "stun:host:0",
         "stun:host:65536", "stun:host:abc", "stun:host:1234567890123456789",
         "stun:[::1", "stun:[]", "http:host", "stuns:host", "stun:ho st"}) {
        json p = {{"sessionId", "id"}, {"iceConfig", {{"iceServers", json::array({{{"urls", url}}})}}}};
        EXPECT_THROW(parse_connection_params(p, start_request("http://host/start")), InvalidTransportParamsError) << url;
    }
}
TEST(ConnectionParams, CamelWinsAndExplicitHeadersDoNotInherit) {
    auto r = parse_connection_params({{"sessionId", "camel"}, {"session_id", "snake"}}, start_request("http://h/start"));
    EXPECT_EQ(r.offer_url, "http://h/sessions/camel/api/offer");
    r = parse_connection_params({{"webrtcRequestParams", {{"endpoint", "http://h/api/offer"}}}}, start_request("http://h/start"));
    EXPECT_TRUE(r.headers.empty());
}
TEST(Answer, ValidationAndBoundaries) {
    json answer = {{"type", "answer"}, {"sdp", "v=0\r\na=candidate:1 1 UDP 1 127.0.0.1 123 typ host\r\n"}};
    EXPECT_EQ(validate_answer(200, answer.dump()), answer["sdp"]);
    EXPECT_NO_THROW(validate_answer(299, answer.dump()));
    for (long status : {0, 199, 300, 500}) EXPECT_THROW(validate_answer(status, answer.dump()), TransportStartError);
    std::string boundary = answer.dump();
    boundary.resize(32768, ' ');
    EXPECT_NO_THROW(validate_answer(200, boundary));
    boundary += ' ';
    EXPECT_THROW(validate_answer(200, boundary), TransportStartError);
    for (const char* bad : {"null", "[]", "{}", "broken",
        R"({"type":"offer","sdp":"a=candidate:1"})",
        R"({"type":"answer","sdp":5})",
        R"({"type":"answer","sdp":"x=a=candidate:1"})",
        R"({"type":"answer","sdp":"a=candidate:"})"}) {
        EXPECT_THROW(validate_answer(200, bad), TransportStartError);
    }
    answer["sdp"] = std::string("a=candidate:1\0bad", 17);
    EXPECT_THROW(validate_answer(200, answer.dump()), TransportStartError);
    std::string deep = R"({"x":)" + std::string(30, '[') + "0" + std::string(30, ']') + "}";
    EXPECT_THROW(validate_answer(200, deep), TransportStartError);
}
TEST(Candidates, LanCandidateFifteenthAndBundledStructure) {
    std::string section = "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:0\r\n";
    for (int i = 0; i < 14; ++i)
        section += "a=candidate:" + std::to_string(i) + " 1 udp 1 172.17.0.1 9000 typ host\r\n";
    section += "a=candidate:15 1 udp 1 192.168.7.222 9000 typ host\r\na=end-of-candidates\r\n";
    std::string sdp = "v=0\r\na=group:BUNDLE 0 1\r\n" + section +
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:1\r\n" +
        "a=candidate:15 1 UDP 1 192.168.7.222 9000 typ host\r\na=setup:passive";
    auto r = filter_remote_candidates(sdp, ipv4_value("192.168.4.29"), ipv4_value("255.255.252.0"), 0);
    EXPECT_EQ(r.kept, 2u); EXPECT_EQ(r.dropped, 14u);
    EXPECT_EQ(r.sdp, "v=0\r\na=group:BUNDLE 0 1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:0\r\n"
        "a=candidate:15 1 udp 1 192.168.7.222 9000 typ host\r\na=end-of-candidates\r\n"
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:1\r\n"
        "a=candidate:15 1 UDP 1 192.168.7.222 9000 typ host\r\na=setup:passive");
}
TEST(Candidates, PublicPathAlsoKeepsUdpIpv4SrflxAndRelay) {
    std::string sdp = "a=candidate:1 1 udp 1 192.168.7.222 9000 typ host\r\n"
        "a=candidate:2 1 udp 1 203.0.113.7 9000 typ srflx raddr 192.168.7.222 rport 9000\r\n"
        "a=candidate:3 1 tcp 1 203.0.113.7 9000 typ srflx\r\n"
        "a=candidate:4 1 udp 1 203.0.113.9 9000 typ relay\r\n";
    // Off the LAN (hotspot) a relay, when offered, replaces the srflx candidates.
    auto r = filter_remote_candidates(sdp, ipv4_value("172.20.10.2"), ipv4_value("255.255.255.240"), 0, true);
    EXPECT_EQ(r.kept, 1u); EXPECT_EQ(r.dropped, 3u);
    EXPECT_NE(r.sdp.find("203.0.113.9 9000 typ relay"), std::string::npos);
    // Without a relay, the UDP srflx candidate is the reachable one.
    auto direct = sdp.substr(0, sdp.find("a=candidate:4"));
    r = filter_remote_candidates(direct, ipv4_value("172.20.10.2"), ipv4_value("255.255.255.240"), 0, true);
    EXPECT_EQ(r.kept, 1u); EXPECT_EQ(r.dropped, 2u);
    EXPECT_NE(r.sdp.find("203.0.113.7 9000 typ srflx raddr"), std::string::npos);
    EXPECT_THROW(filter_remote_candidates(sdp, ipv4_value("172.20.10.2"), ipv4_value("255.255.255.240"), 0), TransportStartError);
}
TEST(Candidates, SignalingHostExceptionAndRejectNonHostOrIpv6) {
    std::string sdp = "a=candidate:1 1 udp 1 10.0.0.1 123 typ host\r\n"
        "a=candidate:2 1 udp 1 ::1 123 typ host\r\n"
        "a=candidate:3 1 udp 1 10.0.0.1 123 typ srflx\r\n"
        "a=candidate:4 1 tcp 1 10.0.0.1 123 typ host\r\n"
        "a=candidate:5 1 udp 1 bad.local 123 typ host\r\n";
    auto r = filter_remote_candidates(sdp, ipv4_value("192.168.1.2"), ipv4_value("255.255.255.0"), ipv4_value("10.0.0.1"));
    EXPECT_EQ(r.kept, 1u); EXPECT_EQ(r.dropped, 4u);
    EXPECT_THROW(filter_remote_candidates(sdp, ipv4_value("192.168.1.2"), ipv4_value("255.255.255.0"), 0), TransportStartError);
    EXPECT_THROW(ipv4_value("1.2.3.256"), TransportStartError);
    EXPECT_THROW(ipv4_value("1.2.3"), TransportStartError);
    EXPECT_THROW(ipv4_value("1.2.3.4.extra"), TransportStartError);
}
TEST(Setup, AiortcRewriteReturnsExactLengthAndOnlyTouchesAttributes) {
    std::string sdp = "v=0\r\na=setup:actpass\r\nm=application\r\na=setup:actpass\r\na=x:a=setup:actpass\r\n";
    size_t before = sdp.size();
    EXPECT_EQ(rewrite_aiortc_active_offer(sdp), before - 2);
    EXPECT_EQ(sdp, "v=0\r\na=setup:active\r\nm=application\r\na=setup:active\r\na=x:a=setup:actpass\r\n");
    EXPECT_EQ(rewrite_aiortc_active_offer(sdp), before - 2);
    sdp = "a=setup:actpass";
    EXPECT_EQ(rewrite_aiortc_active_offer(sdp), 14u);
    EXPECT_EQ(sdp, "a=setup:active");
}
