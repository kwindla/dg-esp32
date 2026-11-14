"""Run a bot with short-lived Cloudflare TURN credentials in PIPECAT_ICE_SERVERS.

For a board that cannot reach the bot directly (for example on a phone hotspot,
with the bot behind an HTTPS tunnel). The Pipecat runner hands these servers to
the bot's own peer connection and returns them as `iceConfig` from /start; the
firmware keeps one UDP TURN relay only for an https:// bot URL.

    uv run python turn_env.py python demo_bot.py --host 127.0.0.1

Reads CF_TURN_KEY_ID and CF_TURN_API_TOKEN from the environment or from
~/.config/cloudflare-turn.env (override with CF_TURN_ENV). Credentials are never
printed. Do not combine with the runner's --esp32 --host mode, which filters the
answer's candidates down to one host address and would drop the relay.
"""
import json
import os
from pathlib import Path
import sys
import urllib.request

API = "https://rtc.live.cloudflare.com/v1/turn/keys/{}/credentials/generate-ice-servers"
# aiortc gathers every candidate before answering, so a long provider list only
# adds allocations; keep one STUN and the UDP relay the firmware will use.
KEEP = ("stun:stun.cloudflare.com:3478", "turn:turn.cloudflare.com:3478?transport=udp")


def load_env(path):
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.removeprefix("export ").split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    return values


def ice_servers(key_id, token, ttl):
    request = urllib.request.Request(
        API.format(key_id), method="POST", data=json.dumps({"ttl": ttl}).encode(),
        # Cloudflare refuses Python's default User-Agent (error 1010).
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "turn-client/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        servers = json.load(response)["iceServers"]
    result = []
    for server in servers:
        urls = [url for url in server.get("urls", []) if url in KEEP]
        if urls:
            result.append({"urls": urls, **{k: server[k] for k in ("username", "credential") if k in server}})
    if not any(url.startswith("turn:") for server in result for url in server["urls"]):
        raise RuntimeError("Cloudflare returned no UDP TURN server on port 3478")
    return result


def main(argv):
    if not argv:
        sys.exit(__doc__)
    if any(arg == "--esp32" for arg in argv):
        sys.exit("--esp32 filters ICE candidates and would drop the TURN relay")
    file_values = load_env(Path(os.getenv("CF_TURN_ENV", "~/.config/cloudflare-turn.env")).expanduser())
    key_id = os.getenv("CF_TURN_KEY_ID") or file_values.get("CF_TURN_KEY_ID")
    token = os.getenv("CF_TURN_API_TOKEN") or file_values.get("CF_TURN_API_TOKEN")
    if not key_id or not token:
        sys.exit("CF_TURN_KEY_ID and CF_TURN_API_TOKEN are required")
    ttl = int(os.getenv("CF_TURN_TTL", "21600"))
    servers = ice_servers(key_id, token, ttl)
    print(f"turn_env: {sum(len(s['urls']) for s in servers)} ICE URLs, ttl {ttl}s", file=sys.stderr)
    os.environ["PIPECAT_ICE_SERVERS"] = json.dumps(servers)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
