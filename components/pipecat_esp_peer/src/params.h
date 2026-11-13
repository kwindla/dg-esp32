//
// Copyright (c) 2024-2026, Daily
//
// SPDX-License-Identifier: BSD-2-Clause
//

#ifndef PIPECAT_ESP_PEER_PARAMS_H
#define PIPECAT_ESP_PEER_PARAMS_H

#include <pipecat/client.h>

#include <nlohmann/json.hpp>
#include <string_view>

#include <map>
#include <optional>
#include <string>
#include <vector>

namespace pipecat::esp_peer {

struct IceServer { std::string url, username, password; };

// Where to send the offer, and how to connect.
struct ConnectionParams {
    // URL of the offer endpoint.
    std::string offer_url;
    // HTTP headers of the offer.
    std::map<std::string, std::string> headers;
    // Data for the bot, sent with the offer. Null if there's none.
    nlohmann::json request_data;
    // UDP STUN servers (TURN is rejected for LAN mode).
    std::vector<IceServer> ice_servers;
};

// Reads the params given to connect(). `start_request` is the request that
// started the bot, if start_bot() did. Throws InvalidTransportParamsError if
// they're wrong.
ConnectionParams parse_connection_params(
        const nlohmann::json& params,
        const std::optional<APIRequest>& start_request
);

// The offer URL of session `session_id`, started with `start_url`: that URL
// with the `/start` at the end of its path replaced by
// `/sessions/<session_id>/api/offer`. Throws InvalidTransportParamsError if
// its path doesn't end with `/start`.
std::string
session_offer_url(const std::string& start_url, const std::string& session_id);

std::string validate_answer(long status, std::string_view body);

}  // namespace pipecat::esp_peer

#endif

namespace pipecat::esp_peer {
// IPv4 values use dotted-quad numeric order: 192.168.1.2 == 0xc0a80102.
uint32_t ipv4_value(std::string_view address);
struct CandidateFilterResult { std::string sdp; size_t kept = 0, dropped = 0; };
CandidateFilterResult filter_remote_candidates(std::string_view sdp,
    uint32_t device_ip, uint32_t netmask, uint32_t signaling_ip, bool public_path = false);
// aiortc-specific RFC 5763 section 5 deviation. Mutates only complete setup
// attribute lines, preserving line endings; returns the NEW byte length.
size_t rewrite_aiortc_active_offer(std::string& sdp);
}
