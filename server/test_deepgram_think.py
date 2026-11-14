import json

import deepgram_think
from deepgram_think import httpx
import duo_bot


def test_request_is_rewritten_for_think():
    body = {"model": "m", "messages": [], "stream": True, "temperature": 0, "max_completion_tokens": 250,
            "chat_template_kwargs": {"enable_thinking": False}, "stream_options": {"include_usage": True}}
    request = httpx.Request("POST", "https://agent.deepgram.com/v1/chat/completions", json=body,
                            headers={"Authorization": "Token k"})
    out = deepgram_think.think_request(request)
    assert out.url.path == "/v1/think"
    assert json.loads(out.content) == {"model": "m", "messages": [], "stream": True, "temperature": 0,
                                       "stream_options": {"include_usage": True}}
    assert out.headers["authorization"] == "Token k"
    assert int(out.headers["content-length"]) == len(out.content)


def test_only_deepgram_host_selects_think():
    assert deepgram_think.is_think_url("https://agent.deepgram.com/v1")
    assert not deepgram_think.is_think_url("http://127.0.0.1:8080/v1")


def test_make_llm_keeps_settings_for_think(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    config = duo_bot.configuration({"DUO_LLM_URL": deepgram_think.BASE_URL, "DUO_LLM_MODEL": deepgram_think.MODEL})
    llm = duo_bot.make_llm(config)
    assert isinstance(llm, deepgram_think.DeepgramThinkLLMService)
    assert duo_bot.llm_invariants(llm)["max_completion_tokens"] == 250
    local = duo_bot.make_llm(duo_bot.configuration({"DUO_LLM_URL": "http://127.0.0.1:8080/v1"}))
    assert isinstance(local, duo_bot.OpenAILLMService)
    assert not isinstance(local, deepgram_think.DeepgramThinkLLMService)


async def test_client_sends_token_auth_and_streams(monkeypatch):
    seen = []
    chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
    ]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    original = deepgram_think.ThinkTransport.__init__
    monkeypatch.setattr(deepgram_think.ThinkTransport, "__init__",
                        lambda self, inner=None: original(self, httpx.MockTransport(handler)))
    service = deepgram_think.DeepgramThinkLLMService(
        api_key="dg-key", base_url=deepgram_think.BASE_URL,
        settings=deepgram_think.OpenAILLMService.Settings(model=deepgram_think.MODEL))
    stream = await service._client.chat.completions.create(
        model=deepgram_think.MODEL, messages=[{"role": "user", "content": "hi"}], stream=True,
        max_completion_tokens=10)
    text = "".join([c.choices[0].delta.content or "" async for c in stream])
    assert text == "Hello"
    (request,) = seen
    assert request.url.path == "/v1/think"
    assert request.headers["authorization"] == "Token dg-key"
    assert "max_completion_tokens" not in json.loads(request.content)


def test_non_deepgram_endpoint_keeps_plain_client():
    llm = duo_bot.make_llm(duo_bot.configuration({"DUO_LLM_URL": "http://127.0.0.1:8080/v1"}))
    assert not isinstance(llm._client._client._transport, deepgram_think.ThinkTransport)


def test_http_deepgram_url_is_refused():
    import pytest
    with pytest.raises(ValueError):
        deepgram_think.is_think_url("http://agent.deepgram.com/v1")


def test_missing_deepgram_key_is_a_clear_error(monkeypatch):
    import pytest
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    config = duo_bot.configuration({"DUO_LLM_URL": deepgram_think.BASE_URL, "DUO_LLM_MODEL": deepgram_think.MODEL})
    with pytest.raises(RuntimeError, match="DEEPGRAM_API_KEY"):
        duo_bot.make_llm(config)
