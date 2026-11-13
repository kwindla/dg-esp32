#pragma once
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
namespace pipecat::esp_peer {
inline bool wire_start_safe(const nlohmann::json& request) {
    if(!request.is_object() || request.size()!=2 || !request.contains("transport") || request["transport"]!="webrtc" ||
       !request.contains("body"))return false;
    const auto& body=request["body"];
    if(!body.is_object() || body.size()!=1 || !body.contains("audio"))return false;
    const auto& audio=body["audio"];
    if(!audio.is_object() || audio.size()!=4 || !audio.contains("v") || !audio.contains("aec") ||
       !audio.contains("ref_gain_db") || !audio.contains("guard_ms"))return false;
    if(!audio["v"].is_number_integer() || audio["v"]!=1 || !audio["aec"].is_boolean() ||
       !audio["ref_gain_db"].is_number_integer() || !audio["guard_ms"].is_number_integer())return false;
    const auto gain=audio["ref_gain_db"].get<int64_t>(),guard=audio["guard_ms"].get<int64_t>();
    return gain>=0 && gain<=33 && gain%3==0 && guard>=0 && guard<=1000;
}
// Refusal diagnostics reveal top-level key names only, never values/nested data.
inline nlohmann::json wire_start_keys(const nlohmann::json& request) {
    auto names=nlohmann::json::array();
    if(request.is_object())for(auto i=request.begin();i!=request.end();++i)names.push_back(i.key());
    return names;
}
inline nlohmann::json wire_sdp(std::string_view text) {
    auto lines=nlohmann::json::array();bool media=false;
    for(size_t at=0;at<text.size();) {
        auto end=text.find('\n',at);if(end==text.npos)end=text.size();
        auto line=text.substr(at,end-at);if(!line.empty() && line.back()=='\r')line.remove_suffix(1);
        if(line.substr(0,2)=="m="){media=true;lines.push_back(std::string(line));}
        else if(media)for(const auto* prefix:{"a=rtpmap:","a=fmtp:","a=ptime:","a=sctp-port:","a=max-message-size:"})
            if(line.substr(0,std::char_traits<char>::length(prefix))==prefix){lines.push_back(std::string(line));break;}
        at=end==text.size()?end:end+1;
    }
    return lines;
}
inline nlohmann::json wire_schema(const nlohmann::json& value) {
    if(value.is_object()) {
        auto result=nlohmann::json::object();
        for(auto i=value.begin();i!=value.end();++i)result[i.key()]=wire_schema(i.value());
        return result;
    }
    if(value.is_number_integer())return "integer";
    if(value.is_number())return "number";
    if(value.is_boolean())return "boolean";
    if(value.is_string())return "string";
    if(value.is_null())return "null";
    return "array";
}
enum class WireMessage : unsigned {None=0,Ready=1,Pong=2,DisplayReady=4,DisplayResult=8};
inline WireMessage wire_message_kind(const std::string& type,const nlohmann::json& data) {
    if(type=="client-ready")return WireMessage::Ready;
    if(type!="client-message" || !data.is_object())return WireMessage::None;
    const auto name=data.value("t",std::string{});
    return name=="pong"?WireMessage::Pong:name=="display-ready"?WireMessage::DisplayReady:
        name=="display-result"?WireMessage::DisplayResult:WireMessage::None;
}
inline std::pair<std::string,nlohmann::json> wire_message_record(const nlohmann::json& message) {
    const auto& data=message.at("data");
    if(message.at("type")=="client-ready")return {"client_ready",data};
    const auto name=data.at("t").get<std::string>();const auto& body=data.at("d");
    nlohmann::json result={{"schema",wire_schema(body)}};
    if(name=="display-ready")result["values"]={{"v",body.at("v")},{"wire_bytes",body.at("wire_bytes")},{"max_ops",body.at("max_ops")}};
    if(name=="display-result")result["values"]={{"id",body.at("id")}};
    return {"msg="+name,result};
}
}
