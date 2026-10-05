"""Exceptions for the Rituals Perfume Genie API."""


class RitualsGenieError(Exception):
    """Generic Rituals Perfume Genie exception."""


class RitualsGenieConnectionError(RitualsGenieError):
    """Rituals Perfume Genie connection exception (base for connectivity issues)."""


class RitualsGenieConnectionTimeoutError(RitualsGenieConnectionError):
    """Rituals Perfume Genie connection timeout."""


class RitualsGenieResponseError(RitualsGenieError):
    """Rituals Perfume Genie unexpected/error HTTP response from the API.

    The HTTP status, if any, is in `status`.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Initialize, with the HTTP status of the response, if any."""
        super().__init__(message)
        self.status = status


class RitualsGenieRateLimitError(RitualsGenieResponseError):
    """Rituals Perfume Genie API rate limit hit."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        status: int | None = None,
    ) -> None:
        """Initialize, with the seconds to wait before trying again, if known."""
        super().__init__(message, status=status)
        self.retry_after = retry_after


class RitualsGenieAuthenticationError(RitualsGenieError):
    """Rituals Perfume Genie authentication exception (invalid credentials)."""


class RitualsGenieValueError(RitualsGenieError, ValueError):
    """Rituals Perfume Genie invalid argument, caught before calling the API."""
