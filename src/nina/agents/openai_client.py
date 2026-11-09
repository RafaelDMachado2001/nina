"""A small client for OpenAI-compatible Chat Completions APIs. Standard library only (urllib), blocking: call it from a
worker thread. Works with OpenAI, Google Gemini's compatible endpoint, Ollama, OpenRouter, Groq, LM Studio and others.
"""
import json
import socket
import urllib.error
import urllib.request

from nina.i18n import _

USER_AGENT = "NINA (+https://github.com/RafaelDMachado2001/nina)"


class ApiError(Exception):
    """`status` is the HTTP status (0: no answer: timeout, refused, DNS...). `message` is the provider's own text."""

    def __init__(self, status: int, message: str, retry_after: str = "", timeout: float = 0.0):
        super().__init__(f"{status}: {message}")
        self.status, self.message, self.retry_after, self.timeout = status, message, retry_after, timeout


def _provider_message(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return " ".join(text.split())[:400]
    if isinstance(data, list) and data:          # Gemini wraps errors in a list
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:400]
        if isinstance(err, str):
            return err[:400]
    return " ".join(text.split())[:400]


def _request(url: str, key: str, body: dict | None, timeout: float, headers: dict | None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    for name, value in (headers or {}).items():
        req.add_header(str(name), str(value))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, _provider_message(e.read() or b"") or str(e.reason),
                       e.headers.get("Retry-After", "") if e.headers else "") from None
    except (socket.timeout, TimeoutError):
        raise ApiError(0, "timeout", timeout=timeout) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise ApiError(0, "timeout", timeout=timeout) from None
        raise ApiError(0, str(e.reason)) from None
    except OSError as e:
        raise ApiError(0, str(e)) from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ApiError(200, _("The server did not answer with JSON: {text}", text=" ".join(raw.decode("utf-8", "replace").split())[:200])) from None


def chat(base_url: str, key: str, model: str, messages: list, tools: list, timeout: float, headers: dict) -> dict:
    """POST {base_url}/chat/completions (not streamed). Returns the raw response; raises ApiError."""
    body = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    return _request(base_url.rstrip("/") + "/chat/completions", key, body, timeout, headers)


def list_models(base_url: str, key: str, timeout: float = 20, headers: dict | None = None) -> list:
    """Model ids from GET {base_url}/models (Gemini's "models/" prefix removed), sorted."""
    data = _request(base_url.rstrip("/") + "/models", key, None, timeout, headers)
    items = data.get("data", []) if isinstance(data, dict) else []
    ids = {str(m.get("id", "")).removeprefix("models/") for m in items if isinstance(m, dict) and m.get("id")}
    return sorted(i for i in ids if i)


def friendly(err: ApiError) -> str:
    """A sentence for the user that keeps the provider's own message."""
    if err.status == 0:
        if err.message == "timeout":
            return _("No answer within {seconds}s.", seconds=f"{err.timeout:g}")
        return _("Could not reach the server: {message}", message=err.message)
    if err.status in (401, 403):
        return _("The API key was refused ({status}): {message}", status=err.status, message=err.message)
    if err.status == 404:
        return _("Not found ({status}): {message}", status=err.status, message=err.message)
    if err.status == 429:
        retry = _(" (try again in {seconds}s)", seconds=err.retry_after) if err.retry_after else ""
        return _("Quota or rate limit reached{retry}: {message}", retry=retry, message=err.message)
    return _("The server answered {status}: {message}", status=err.status, message=err.message)
