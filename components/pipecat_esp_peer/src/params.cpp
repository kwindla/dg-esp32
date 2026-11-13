//
// Copyright (c) 2024-2026, Daily
//
// SPDX-License-Identifier: BSD-2-Clause
//

#include "params.h"
#include "msg_queue.h"

#include <pipecat/errors.h>

#include <exception>
#include <array>

using nlohmann::json;

namespace pipecat::esp_peer {

namespace {

// The value of `key` in `object`, in camel case or in snake case. Null if
// it's not there.
const json* find(const json& object, const char* camel, const char* snake) {
    for (const char* key: {camel, snake}) {
        auto it = object.find(key);
        if (it != object.end()) {
            return &*it;
        }
    }
    return nullptr;
}

const json* find(const json& object, const char* key) {
    return find(object, key, key);
}

std::string string_value(const json& value, const std::string& name) {
    if (!value.is_string()) {
        throw InvalidTransportParamsError("`" + name + "` must be a string");
    }
    return value.get<std::string>();
}

std::vector<std::string> ice_server_urls(const json& server) {
    const json* urls = find(server, "urls");
    if (urls == nullptr) {
        throw InvalidTransportParamsError("ICE servers need their `urls`");
    }
    if (urls->is_string()) {
        return {urls->get<std::string>()};
    }
    std::vector<std::string> result;
    if (urls->is_array()) {
        for (const json& url: *urls) {
            result.push_back(string_value(url, "urls"));
        }
    }
    if (result.empty()) {
        throw InvalidTransportParamsError(
                "An ICE server's `urls` must be a URL, or a list of them"
        );
    }
    return result;
}

// Returns false for a relay URL the device does not use (TLS or TCP TURN).
bool ice_server(std::string url, bool public_path, IceServer& out) {
    const bool turn = url.rfind("turn:", 0) == 0;
    if (turn || url.rfind("turns:", 0) == 0) {
        // LAN mode is STUN only. A public (HTTPS) bot may hand out UDP TURN,
        // the only path through a cellular NAT (duo demo over a hotspot).
        if (!public_path) throw InvalidTransportParamsError("TURN not supported in LAN mode");
        if (!turn) return false;
        auto query = url.find('?');
        if (query != std::string::npos) {
            if (url.substr(query) != "?transport=udp") return false;
            url.resize(query);
        }
    }
    const size_t scheme = 5;  // "stun:" and "turn:"
    if ((!turn && url.rfind("stun:", 0) != 0) || url.size() <= scheme ||
        url.find_first_of(" /?#@\\\t\r\n") != std::string::npos) {
        throw InvalidTransportParamsError("Invalid ICE server URL");
    }
    std::string host = url.substr(scheme);
    std::string port;
    if (host.front() == '[') {
        auto end = host.find(']');
        if (end == std::string::npos || end == 1)
            throw InvalidTransportParamsError("Invalid ICE server host");
        if (end + 1 < host.size()) {
            if (host[end + 1] != ':')
                throw InvalidTransportParamsError("Invalid ICE server port");
            port = host.substr(end + 2);
            if (port.empty()) throw InvalidTransportParamsError("Empty STUN port");
        }
    } else {
        auto colon = host.find(':');
        if (colon != std::string::npos) {
            port = host.substr(colon + 1);
            host.resize(colon);
            if (port.empty()) throw InvalidTransportParamsError("Empty STUN port");
        }
        if (host.empty()) throw InvalidTransportParamsError("Empty STUN host");
    }
    if (!port.empty()) {
        if (port.size() > 5 || port.find_first_not_of("0123456789") != std::string::npos ||
            std::stoul(port) == 0 || std::stoul(port) > 65535)
            throw InvalidTransportParamsError("Invalid ICE server port");
    }
    out = {url, {}, {}};
    return true;
}

std::vector<IceServer> ice_servers(const json& ice_config, bool public_path) {
    if (!ice_config.is_object()) {
        throw InvalidTransportParamsError("`iceConfig` must be a JSON object");
    }
    const json* servers = find(ice_config, "iceServers", "ice_servers");
    if (servers == nullptr) {
        return {};
    }
    if (!servers->is_array()) {
        throw InvalidTransportParamsError("`iceServers` must be a list");
    }

    std::vector<IceServer> result;
    unsigned relays = 0;
    for (const json& server: *servers) {
        if (!server.is_object()) {
            throw InvalidTransportParamsError(
                    "ICE servers must be JSON objects"
            );
        }
        const json* username = find(server, "username");
        const json* credential = find(server, "credential");
        for (const std::string& url: ice_server_urls(server)) {
            IceServer parsed;
            if (!ice_server(url, public_path, parsed)) continue;
            if (username != nullptr) {
                parsed.username = string_value(*username, "username");
            }
            if (credential != nullptr) {
                parsed.password = string_value(*credential, "credential");
            }
            if (parsed.url.rfind("turn:", 0) == 0) {
                if (parsed.username.empty() || parsed.password.empty())
                    throw InvalidTransportParamsError("TURN needs a username and credential");
                // One relay allocation is enough; providers list several ports.
                if (relays++) continue;
            }
            result.push_back(parsed);
        }
    }
    return result;
}

}  // namespace

ConnectionParams parse_connection_params(
        const json& params,
        const std::optional<APIRequest>& start_request
) {
    if (!params.is_object()) {
        throw InvalidTransportParamsError(
                "SmallWebRTC connection params must be a JSON object"
        );
    }

    ConnectionParams result;
    if (const json* request =
                find(params, "webrtcRequestParams", "webrtc_request_params")) {
        if (!request->is_object()) {
            throw InvalidTransportParamsError(
                    "`webrtcRequestParams` must be a JSON object"
            );
        }
        const json* endpoint = find(*request, "endpoint");
        if (endpoint != nullptr) {
            result.offer_url = string_value(*endpoint, "endpoint");
        }
        if (result.offer_url.empty()) {
            throw InvalidTransportParamsError(
                    "`webrtcRequestParams` needs the offer URL, in `endpoint`"
            );
        }
        if (const json* headers = find(*request, "headers")) {
            if (!headers->is_object()) {
                throw InvalidTransportParamsError(
                        "`headers` must be a JSON object"
                );
            }
            for (const auto& [name, value]: headers->items()) {
                result.headers[name] = string_value(value, name);
            }
        }
        if (const json* data = find(*request, "requestData", "request_data")) {
            result.request_data = *data;
        }
    } else if (const json* session_id =
                       find(params, "sessionId", "session_id")) {
        if (!start_request) {
            throw InvalidTransportParamsError(
                    "A `sessionId` only works after start_bot(), which tells "
                    "where to send the offer. Without it, use "
                    "`webrtcRequestParams`"
            );
        }
        result.offer_url = session_offer_url(
                start_request->endpoint, string_value(*session_id, "sessionId")
        );
        result.headers = start_request->headers;
    } else {
        throw InvalidTransportParamsError(
                "SmallWebRTC connection params need a `sessionId` from "
                "start_bot(), or `webrtcRequestParams`"
        );
    }

    if (const json* ice_config = find(params, "iceConfig", "ice_config")) {
        result.ice_servers = ice_servers(*ice_config, start_request && start_request->endpoint.rfind("https://", 0) == 0);
    }
    return result;
}

std::string
session_offer_url(const std::string& start_url, const std::string& session_id) {
    // The path starts after the scheme and the host, and ends at the query or
    // the fragment.
    size_t scheme_end = start_url.find("://");
    size_t path_begin = start_url.find(
            '/', scheme_end == std::string::npos ? 0 : scheme_end + 3
    );
    size_t path_end = start_url.find_first_of("?#");
    if (path_end == std::string::npos) {
        path_end = start_url.size();
    }

    const std::string start = "/start";
    if (path_begin == std::string::npos || path_end < path_begin ||
        path_end - path_begin < start.size() ||
        start_url.compare(path_end - start.size(), start.size(), start) != 0) {
        throw InvalidTransportParamsError(
                "The start URL doesn't end with /start, so the offer URL is unknown. Use "
                "`webrtcRequestParams`"
        );
    }

    return start_url.substr(0, path_end - start.size()) + "/sessions/" +
           session_id + "/api/offer" + start_url.substr(path_end);
}

std::string validate_answer(long status, std::string_view body) {
    if (status < 200 || status >= 300)
        throw TransportStartError("Offer HTTP status " + std::to_string(status));
    if (body.size() > 32768) throw TransportStartError("SDP answer exceeds 32 KiB");
    // Bound nesting before the JSON parser can build an arbitrarily deep tree.
    if (!valid_message_json(body, 16)) throw TransportStartError("Invalid answer JSON");
    auto answer = json::parse(body);
    if (!answer.contains("sdp") || !answer["sdp"].is_string() ||
        !answer.contains("type") || answer["type"] != "answer")
        throw TransportStartError("Expected SDP answer");
    auto sdp = answer["sdp"].get<std::string>();
    bool candidate = false;
    size_t pos = 0;
    while (pos < sdp.size()) {
        auto end = sdp.find('\n', pos);
        auto line = sdp.substr(pos, end == std::string::npos ? end : end - pos);
        if (line.rfind("a=candidate:", 0) == 0 && line.size() > 12 && line[12] != '\r')
            candidate = true;
        if (end == std::string::npos) break;
        pos = end + 1;
    }
    if (!candidate || sdp.find('\0') != std::string::npos)
        throw TransportStartError("SDP answer has no candidates or contains NUL");
    return sdp;
}

}  // namespace pipecat::esp_peer

namespace pipecat::esp_peer {
uint32_t ipv4_value(std::string_view address) {
    uint32_t result = 0;
    size_t pos = 0;
    for (int i = 0; i < 4; ++i) {
        unsigned value = 0, digits = 0;
        while (pos < address.size() && address[pos] >= '0' && address[pos] <= '9') {
            value = value * 10 + (address[pos++] - '0');
            if (++digits > 3 || value > 255) throw TransportStartError("Invalid IPv4 address");
        }
        if (!digits) throw TransportStartError("Invalid IPv4 address");
        result = (result << 8) | value;
        if (i < 3 && (pos == address.size() || address[pos++] != '.'))
            throw TransportStartError("Invalid IPv4 address");
    }
    if (pos != address.size()) throw TransportStartError("Invalid IPv4 address");
    return result;
}
CandidateFilterResult filter_remote_candidates(std::string_view sdp,
    uint32_t device_ip, uint32_t netmask, uint32_t signaling_ip, bool public_path) {
    CandidateFilterResult result;
    // Over a cellular NAT the device spends its whole connect window on srflx
    // pairs the carrier drops and never reaches the relay pair (hotspot run,
    // 2026-11-06). When the bot offers a relay, use it instead of its srflx.
    const bool relay_offered = public_path &&
        sdp.find(" typ relay") != std::string_view::npos;
    // Preserve every non-candidate byte, including final lines without CRLF.
    for (size_t pos = 0; pos < sdp.size();) {
        size_t end = sdp.find('\n', pos);
        size_t next = end == std::string_view::npos ? sdp.size() : end + 1;
        auto line = sdp.substr(pos, next - pos);
        bool keep = true;
        if (line.substr(0, 12) == "a=candidate:") {
            std::array<std::string_view, 8> fields{};
            size_t at = 0, n = 0;
            while (n < fields.size() && at < line.size()) {
                at = line.find_first_not_of(" \t\r\n", at);
                if (at == std::string_view::npos) break;
                size_t to = line.find_first_of(" \t\r\n", at);
                if (to == std::string_view::npos) to = line.size();
                fields[n++] = line.substr(at, to - at); at = to;
            }
            keep = false;
            // A public (HTTPS) bot is off the LAN: only its server-reflexive and
            // relay addresses are reachable (duo demo over a hotspot).
            const bool srflx = public_path && (fields[7] == "relay" || (fields[7] == "srflx" && !relay_offered));
            if (n == 8 && fields[6] == "typ" && (fields[7] == "host" || srflx) &&
                (fields[2] == "udp" || fields[2] == "UDP")) {
                try {
                    auto ip = ipv4_value(fields[4]);
                    keep = ip && (srflx || (netmask && (ip & netmask) == (device_ip & netmask)) ||
                                   (signaling_ip && ip == signaling_ip));
                } catch (const TransportStartError&) {}
            }
            if (keep) ++result.kept; else ++result.dropped;
        }
        if (keep) result.sdp.append(line);
        pos = next;
    }
    if (!result.kept) throw TransportStartError(public_path ? "No reachable IPv4 host, srflx or relay candidates" : "No reachable IPv4 host candidates");
    // The binary has a remote cap of ten. Refuse rather than silently truncate
    // potentially usable bundled candidates if a host has many LAN interfaces.
    if (result.kept > 10) throw TransportStartError("More than ten reachable remote candidates");
    return result;
}
size_t rewrite_aiortc_active_offer(std::string& sdp) {
    for (size_t pos = 0; pos < sdp.size();) {
        auto end = sdp.find('\n', pos);
        size_t length = (end == std::string::npos ? sdp.size() : end) - pos;
        if (length && sdp[pos + length - 1] == '\r') --length;
        if (sdp.compare(pos, length, "a=setup:actpass") == 0) {
            sdp.replace(pos, length, "a=setup:active");
            if (end != std::string::npos) --end;
        }
        if (end == std::string::npos) break;
        pos = end + 1;
    }
    return sdp.size();
}
}
