class TelegramUnavailable(RuntimeError):
    """Telegram could not provide authoritative state; retry later."""

    def __init__(self, retry_after: int | None = None):
        super().__init__("Telegram is temporarily unavailable")
        self.retry_after = retry_after


class AccountChanged(RuntimeError):
    """The request began under an account that has since been replaced."""


class LogUnavailable(RuntimeError):
    """Action or event history could not be stored or retrieved."""


class ReactionBlocked(RuntimeError):
    """A reaction was not queued because an earlier write is unresolved."""
