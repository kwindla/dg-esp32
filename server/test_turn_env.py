import io
import json
import urllib.error

import pytest

import turn_env

SERVERS = [
    {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turns:turn.cloudflare.com:443?transport=tcp"],
     "username": "u", "credential": "c"},
]


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_keeps_one_stun_and_the_udp_relay(monkeypatch):
    sent = []
    monkeypatch.setattr(turn_env.urllib.request, "urlopen",
                        lambda request, timeout: sent.append(request) or Response(json.dumps({"iceServers": SERVERS}).encode()))
    result = turn_env.ice_servers("kid", "tok", 60)
    assert result == [{"urls": ["stun:stun.cloudflare.com:3478"]},
                      {"urls": ["turn:turn.cloudflare.com:3478?transport=udp"], "username": "u", "credential": "c"}]
    assert sent[0].get_header("User-agent") == "turn-client/1.0"


def test_no_udp_relay_is_an_error(monkeypatch):
    monkeypatch.setattr(turn_env.urllib.request, "urlopen",
                        lambda request, timeout: Response(json.dumps({"iceServers": SERVERS[:1]}).encode()))
    with pytest.raises(RuntimeError):
        turn_env.ice_servers("kid", "tok", 60)


def _main(monkeypatch, capsys, argv, error):
    monkeypatch.setenv("CF_TURN_ENV", "/nonexistent")
    monkeypatch.setenv("CF_TURN_KEY_ID", "kid")
    monkeypatch.setenv("CF_TURN_API_TOKEN", "secret-token")
    def fail(*a, **k):
        raise error
    monkeypatch.setattr(turn_env, "ice_servers", fail)
    monkeypatch.setattr(turn_env.os, "execvp", lambda *a: pytest.fail("must not exec"))
    with pytest.raises(SystemExit) as exit_info:
        turn_env.main(argv)
    captured = capsys.readouterr()
    return str(exit_info.value) + captured.out + captured.err


@pytest.mark.parametrize("error", [ValueError("Invalid header value b'Bearer secret-token\\n'"),
                                   urllib.error.HTTPError("u", 403, "Forbidden", {}, None)])
def test_request_failure_never_prints_the_token(monkeypatch, capsys, error):
    output = _main(monkeypatch, capsys, ["true"], error)
    assert "secret-token" not in output
    assert "credential request failed" in output


def test_bad_token_characters_refused_without_echo(monkeypatch, capsys):
    monkeypatch.setenv("CF_TURN_ENV", "/nonexistent")
    monkeypatch.setenv("CF_TURN_KEY_ID", "kid")
    monkeypatch.setenv("CF_TURN_API_TOKEN", "secret\ntoken")
    with pytest.raises(SystemExit) as exit_info:
        turn_env.main(["true"])
    assert "unexpected characters" in str(exit_info.value)
    assert "secret" not in str(exit_info.value) + "".join(capsys.readouterr())


def test_esp32_mode_refused(monkeypatch):
    with pytest.raises(SystemExit):
        turn_env.main(["python", "bot.py", "--esp32"])
