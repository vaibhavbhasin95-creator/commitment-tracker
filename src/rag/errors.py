class RagError(RuntimeError):
    """Custom exception for all RAG-related failures (bad keys, model errors, timeouts)."""
    pass


class MeetingNotFound(RagError):
    """The meeting to index was deleted before indexing ran."""


class RagConsentRequired(RagError):
    """A usable cloud engine is saved, but the user hasn't yet confirmed that their
    meetings may be sent to it. The Ask page shows a one-time confirmation for this."""

    def __init__(self, message: str, provider: str):
        super().__init__(message)
        self.provider = provider