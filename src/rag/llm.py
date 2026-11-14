import litellm

from .errors import RagError

CHAT_TIMEOUT_SECONDS = 120
# Some servers (GPT4All's, for one) stop after about 20 tokens unless told how long a reply may be.
DEFAULT_MAX_TOKENS = 600
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"

# Cache the embedding model globally so it only loads once in memory.
_embedding_model = None


def _get_embedding_model():
    # Imported here: the ML libraries are slow to load, and the app shouldn't
    # pay for them at startup when RAG isn't used.
    from sentence_transformers import SentenceTransformer
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _embedding_model


def _get_litellm_args(settings: dict) -> tuple[str, dict]:
    """
    Private helper: translates the account's saved AI engine settings (the shape
    ai_extraction.get_settings returns) into a LiteLLM model string and keyword arguments.
    """
    if not settings or not settings.get("consent_at"):
        raise RagError("Choose an AI engine first and provide consent.")

    engine = settings.get("engine")
    config = settings.get("config") or {}
    model_name = (config.get("model") or "").strip()
    if engine in ("anthropic", "openai_compatible") and not model_name:
        raise RagError("No model is saved for this AI engine.")

    if engine == "anthropic":
        return f"anthropic/{model_name}", {"api_key": config.get("api_key")}
    if engine == "openai_compatible":
        # A keyless local server (GPT4All, Ollama) stores an empty key, but the
        # OpenAI client inside LiteLLM refuses to start without one.
        return f"openai/{model_name}", {
            "api_base": config.get("base_url"),
            "api_key": config.get("api_key") or "local",
        }
    raise RagError(f"Unsupported or missing AI engine: {engine}")


def _friendly_error(e: Exception) -> str:
    """One readable message per failure kind, without echoing request details."""
    errors = litellm.exceptions
    unreachable = "Couldn't reach the AI provider. Check the base URL and that the server is running."
    if isinstance(e, errors.AuthenticationError):
        return "The AI provider rejected the API key."
    if isinstance(e, errors.NotFoundError):
        return "The AI provider doesn't know that model name. Check the saved model."
    if isinstance(e, errors.RateLimitError):
        return "Rate limited by the AI provider. Try again shortly."
    if isinstance(e, errors.Timeout):
        return "The AI provider took too long to reply. A local model may still be loading; try again."
    if isinstance(e, errors.APIConnectionError):
        return unreachable
    # LiteLLM reports a refused connection as an InternalServerError whose text says so.
    if isinstance(e, errors.InternalServerError) and "connection error" in str(e).lower():
        return unreachable
    return f"AI generation failed ({type(e).__name__})."


def chat(settings: dict, messages: list[dict], temperature: float = 0, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """
    Public function: sends messages to the configured LLM via LiteLLM and returns
    the response text, at most max_tokens long. Raises RagError on any failure,
    including a reply that was cut off at that length.
    """
    model, extra_args = _get_litellm_args(settings)
    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=CHAT_TIMEOUT_SECONDS,
            **extra_args,
        )
    except Exception as e:
        raise RagError(_friendly_error(e)) from e

    choice = response.choices[0]  # type: ignore[union-attr]  (non-streaming call)
    if choice.finish_reason == "length":
        raise RagError("The AI's answer was cut off because it hit the maximum reply length. Try a more specific question.")
    if not choice.message.content:
        raise RagError("The AI provider returned an empty reply.")
    return choice.message.content


def embed(texts: list[str]) -> list[list[float]]:
    """
    Public function: wraps sentence-transformers, loads the model once, and
    returns plain Python lists of vectors. Raises RagError if the model can't load.
    """
    try:
        model = _get_embedding_model()
        # Normalised vectors make cosine similarity a plain dot product (Step 1.4).
        embeddings = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
    except Exception as e:
        raise RagError(f"Embedding failed ({type(e).__name__}). On first use the model needs a download.") from e
    return embeddings.tolist()
