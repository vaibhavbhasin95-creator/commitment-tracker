"""RAG's AI call (rag/llm.py chat): reply length limit, cut-off replies, empty replies and error messages.
litellm.completion is replaced by a stub: no model is called and nothing leaves the machine."""

from types import SimpleNamespace

from _support import ok

import litellm
import rag.llm as llm
from rag.errors import RagError

SETTINGS = {"engine": "openai_compatible", "consent_at": "local",
            "config": {"base_url": "http://localhost:4891/v1", "model": "Test Model", "api_key": ""}}
MESSAGES = [{"role": "user", "content": "hello"}]
calls = []


def reply(content="Hello there", finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))])


def stub(response=None, error=None):
    def completion(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return response or reply()
    litellm.completion = completion


def attempt(**kwargs):
    try:
        return llm.chat(SETTINGS, MESSAGES, **kwargs)
    except RagError as e:
        return e


stub()
ok(attempt() == "Hello there", "a normal reply is returned as text")
ok(calls[-1]["max_tokens"] == llm.DEFAULT_MAX_TOKENS == 600, "a maximum reply length is always sent (some servers stop after ~20 tokens without one)")
attempt(max_tokens=50)
ok(calls[-1]["max_tokens"] == 50, "the caller can choose a different limit")
ok(calls[-1]["model"] == "openai/Test Model" and calls[-1]["api_base"] == "http://localhost:4891/v1" and calls[-1]["temperature"] == 0,
   "the model, endpoint and temperature are passed through as before")

stub(reply("Sourabh is", finish_reason="length"))
cut = attempt()
ok(isinstance(cut, RagError) and "cut off" in str(cut), "a reply that hit the length limit is reported, not returned as if complete")
ok("Sourabh is" not in str(cut), "...without echoing the partial text into the error")

stub(reply(None))
ok(isinstance(attempt(), RagError) and "empty reply" in str(attempt()), "an empty reply is reported")
stub(reply(""))
ok(isinstance(attempt(), RagError), "a blank reply is reported")
stub(reply("Fine", finish_reason="stop"))
ok(attempt() == "Fine", "a reply that finished normally is returned")

for error, expected in [(litellm.exceptions.AuthenticationError("x", "openai", "m"), "rejected the API key"),
                        (litellm.exceptions.NotFoundError("x", "m", "openai"), "doesn't know that model"),
                        (litellm.exceptions.RateLimitError("x", "openai", "m"), "Rate limited"),
                        (litellm.exceptions.Timeout("x", "m", "openai"), "took too long"),
                        (litellm.exceptions.APIConnectionError("x", "openai", "m"), "Couldn't reach"),
                        (RuntimeError("secret detail"), "AI generation failed (RuntimeError)")]:
    stub(error=error)
    got = attempt()
    ok(isinstance(got, RagError) and expected in str(got), f"{type(error).__name__} -> a readable message")
ok("secret detail" not in str(attempt()), "an unexpected error's details aren't echoed")
