"""Best-effort lifecycle signals for the external monitoring dead man's switch."""

import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import UUID

LOGGER = logging.getLogger(__name__)


class HealthchecksClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 2) -> None:
        parsed = urlsplit(base_url.strip())
        if parsed.scheme != "https" or not parsed.netloc or not parsed.path.strip("/"):
            raise ValueError("HEALTHCHECKS_MONITORING_URL must be a complete HTTPS URL")
        self.base_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), parsed.query, "")
        )
        self.timeout_seconds = timeout_seconds

    def ping(self, run_id: UUID, signal: str = "") -> None:
        """Send a start, success, or failure signal without interrupting the worker."""
        parsed = urlsplit(self.base_url)
        path = parsed.path if not signal else f"{parsed.path}/{signal}"
        query = urlencode(
            [*parse_qsl(parsed.query, keep_blank_values=True), ("rid", str(run_id))]
        )
        url = urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))
        try:
            request = Request(url, method="HEAD")
            with urlopen(request, timeout=self.timeout_seconds):
                pass
        except OSError as error:
            LOGGER.warning(
                "healthchecks_ping_failed signal=%s error_type=%s",
                signal or "success",
                type(error).__name__,
            )
