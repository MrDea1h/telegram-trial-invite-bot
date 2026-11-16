"""Minimal Telegram Bot API client. The token lives in the URL, so URLs are never logged."""

import httpx


class TgError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: int | None = None):
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after  # seconds, set when Telegram rate-limits the call


class Api:
    def __init__(self, token: str, timeout: float = 15.0):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._http = httpx.Client(timeout=timeout)

    def call(self, method: str, _timeout: float | None = None, **params):
        payload = {key: value for key, value in params.items() if value is not None}
        try:
            response = self._http.post(
                self._base + method, json=payload, timeout=_timeout or self._http.timeout
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # str(exc) may contain the URL with the token: report the type only.
            raise TgError(method, 0, type(exc).__name__) from None
        if not body.get("ok"):
            raise TgError(
                method,
                body.get("error_code", 0),
                body.get("description", ""),
                (body.get("parameters") or {}).get("retry_after"),
            )
        return body.get("result")

    def close(self) -> None:
        self._http.close()
