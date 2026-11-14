"""Pipecat's OpenAI LLM service pointed at Deepgram's hosted PhoneLLM (/v1/think).

The endpoint speaks Chat Completions but differs in three ways, handled at the
HTTP layer so the service's settings (and anything that records them) are
unchanged:

- the path is /v1/think, not /v1/chat/completions;
- it authenticates with ``Authorization: Token <DEEPGRAM_API_KEY>``;
- it rejects unknown request fields, so the body is reduced to ALLOWED
  (max_completion_tokens and vLLM's chat_template_kwargs are dropped).

Early Access: as of 2026-11-13 only ``phonellm-mini-alpha-2-fp8`` is served
there; the ``phonellm-max`` names return "Model not available".
"""
import json
from urllib.parse import urlsplit

from loguru import logger

from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pipecat.services.openai.llm import OpenAILLMService

try:  # openai>=3 sends through httpx2; requests and transports must match it.
    import httpx2 as httpx
except ImportError:
    import httpx

BASE_URL = "https://agent.deepgram.com/v1"
MODEL = "phonellm-mini-alpha-2-fp8"
ALLOWED = frozenset({"model", "temperature", "reasoning_effort", "messages", "tools",
                     "stream", "stream_options", "mip_opt_out"})


def is_think_url(url):
    """True for Deepgram's hosted endpoint; an http:// spelling is refused (the key would go in clear)."""
    if not url or urlsplit(url).hostname != "agent.deepgram.com":
        return False
    if urlsplit(url).scheme != "https":
        raise ValueError("DUO_LLM_URL for agent.deepgram.com must be https://")
    return True


_reported = set()


def think_request(request):
    """The same request rewritten for /v1/think (path and body)."""
    url = request.url
    if url.path.endswith("/chat/completions"):
        url = url.copy_with(path=url.path[:-len("/chat/completions")] + "/think")
    content = request.content
    if content and request.headers.get("content-type", "").startswith("application/json"):
        body = json.loads(content)
        dropped = frozenset(body) - ALLOWED
        if dropped and dropped not in _reported:
            # /v1/think has no token cap: max_completion_tokens is dropped, so replies are not capped.
            _reported.add(dropped)
            logger.warning(f"Deepgram /v1/think does not accept {sorted(dropped)}; sent without them")
        content = json.dumps({k: v for k, v in body.items() if k in ALLOWED}).encode()
    headers = httpx.Headers(request.headers)
    headers.pop("content-length", None)
    return httpx.Request(request.method, url, headers=headers, content=content,
                         extensions=request.extensions)


class ThinkTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner=None):
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        return await self._inner.handle_async_request(think_request(request))

    async def aclose(self):
        await self._inner.aclose()


class DeepgramThinkLLMService(OpenAILLMService):
    """OpenAILLMService for Deepgram /v1/think; api_key is the Deepgram key."""

    def create_client(self, api_key=None, base_url=None, organization=None, project=None,
                      default_headers=None, **kwargs):
        return AsyncOpenAI(
            api_key="unused", base_url=base_url or BASE_URL,
            default_headers={**(default_headers or {}), "Authorization": f"Token {api_key}"},
            http_client=DefaultAsyncHttpxClient(transport=ThinkTransport()))
