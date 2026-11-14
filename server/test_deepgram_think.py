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
