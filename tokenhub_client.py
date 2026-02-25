#!/usr/bin/env python3
"""
TokenHub Client

Thin client that delegates all LLM provider selection and routing to a
TokenHub instance (https://github.com/jordanhubbard/tokenhub).

TokenHub exposes an OpenAI-compatible /v1/chat/completions endpoint and
handles provider arbitration, model selection, cost budgets, latency
constraints, and Thompson-Sampling-based routing internally — removing the
need for this application to maintain its own provider logic.

Public interface is identical to the old MultiHostClient so reviewer.py
requires only a one-line import change.

Configuration (config.yaml):
    tokenhub:
      url: "http://localhost:8090"
      api_key: ""          # Bearer token; or set TOKENHUB_API_KEY env var
      # model_hint: ""     # Optional routing hint; auto-discovered if blank
      timeout: 600         # Request timeout in seconds
      max_tokens: 4096     # Maximum tokens per response
      temperature: 0.1     # Generation temperature

Note: TokenHub's /v1/chat/completions endpoint requires the "model" field.
When model_hint is blank, the client auto-discovers available models at
startup and selects the first one.

Environment variable overrides (take priority over config file):
    TOKENHUB_URL      – override tokenhub.url
    TOKENHUB_API_KEY  – override tokenhub.api_key
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions (same names as the old llm_client.py for drop-in compatibility)
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Base exception for TokenHub / LLM errors."""

class LLMConnectionError(LLMError):
    """Raised when TokenHub is unreachable or returns an unexpected error."""

class LLMModelNotFoundError(LLMError):
    """Raised when no model is routable by TokenHub for the request."""


# ---------------------------------------------------------------------------
# HTTP helpers (reuse the pooled client already in this project)
# ---------------------------------------------------------------------------

try:
    from async_http_client import PooledHTTPClient
    _HTTP_CLIENT_AVAILABLE = True
except ImportError:
    _HTTP_CLIENT_AVAILABLE = False

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

if not _HTTP_CLIENT_AVAILABLE:
    from urllib.request import Request, urlopen


class _SimpleHTTP:
    """
    Minimal HTTP helper used when async_http_client is not importable.
    Prefers httpx for connection pooling; falls back to urllib.
    """

    def __init__(self, timeout: float, max_connections: int = 16):
        self.timeout = timeout
        self._client: Any = None
        if _HTTPX_AVAILABLE:
            limits = httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max(1, max_connections // 2),
                keepalive_expiry=30.0,
            )
            self._client = httpx.Client(
                limits=limits, timeout=timeout, follow_redirects=True
            )

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def json_request(
        self,
        url: str,
        method: str = "GET",
        json_data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        timeout: Optional[float] = None,
    ) -> Dict:
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        body = json.dumps(json_data).encode() if json_data else None
        t = timeout or self.timeout

        if self._client is not None:
            resp = self._client.request(
                method=method, url=url, headers=hdrs, content=body, timeout=t
            )
            if resp.status_code >= 400:
                raise HTTPError(
                    url, resp.status_code, resp.reason_phrase, dict(resp.headers), None
                )
            return resp.json()
        else:
            req = Request(url, data=body, headers=hdrs, method=method)
            with urlopen(req, timeout=t) as r:
                return json.loads(r.read().decode())


# ---------------------------------------------------------------------------
# TokenHubClient
# ---------------------------------------------------------------------------

class TokenHubClient:
    """
    LLM client backed by a TokenHub routing service.

    Maintains the same public interface as the old MultiHostClient:
      - chat(messages, ...)   -> str
      - generate(prompt, ...) -> str
      - get_host_status()     -> List[Dict]
      - list_models()         -> List[str]
      - shutdown()            -> None
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        timeout: int = 600,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        model_hint: str = "",
        max_http_connections: int = 16,
    ):
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._model_hint = model_hint

        if _HTTP_CLIENT_AVAILABLE:
            self._http = PooledHTTPClient(
                max_connections=max_http_connections,
                max_keepalive_connections=max(1, max_http_connections // 2),
                timeout=float(timeout),
            )
        else:
            self._http = _SimpleHTTP(
                timeout=float(timeout), max_connections=max_http_connections
            )

        logger.info(f"TokenHubClient initialised: url={self._url}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _post_json(self, path: str, payload: Dict, timeout: Optional[float] = None) -> Dict:
        url = f"{self._url}{path}"
        hdrs = self._auth_headers()
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
        t = timeout or float(self._timeout)

        try:
            if _HTTP_CLIENT_AVAILABLE:
                _, _, body = self._http.request(
                    url=url, method="POST", headers=hdrs, data=data, timeout=t
                )
                return json.loads(body.decode())
            else:
                return self._http.json_request(
                    url=url, method="POST", json_data=payload,
                    headers=self._auth_headers(), timeout=t,
                )
        except HTTPError as e:
            body = getattr(e, "response_body", None)
            detail = body.decode(errors="replace") if body else str(e)
            if e.code == 401:
                raise LLMConnectionError(
                    f"TokenHub returned 401 Unauthorized — check api_key in config: {detail}"
                ) from e
            if e.code == 404:
                raise LLMModelNotFoundError(
                    f"TokenHub returned 404 — no model routable for this request: {detail}"
                ) from e
            raise LLMConnectionError(
                f"TokenHub HTTP {e.code} at {url}: {detail}"
            ) from e
        except URLError as e:
            raise LLMConnectionError(
                f"Cannot reach TokenHub at {url}: {e}"
            ) from e

    def _get_json(self, path: str, timeout: float = 10.0) -> Dict:
        url = f"{self._url}{path}"
        hdrs = self._auth_headers()
        try:
            if _HTTP_CLIENT_AVAILABLE:
                _, _, body = self._http.request(
                    url=url, method="GET", headers=hdrs, timeout=timeout
                )
                return json.loads(body.decode())
            else:
                return self._http.json_request(
                    url=url, method="GET", headers=hdrs, timeout=timeout
                )
        except (HTTPError, URLError):
            return {}

    # ------------------------------------------------------------------
    # Public interface (matches MultiHostClient)
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Send a chat completion request to TokenHub.

        Args:
            messages: List of {"role": ..., "content": ...} dicts.
            max_tokens: Override configured max_tokens.
            temperature: Override configured temperature.

        Returns:
            The assistant's response text.

        Raises:
            LLMConnectionError: If TokenHub is unreachable or returns an error.
            LLMModelNotFoundError: If no model can be routed for the request.
        """
        payload: Dict[str, Any] = {
            "messages": messages,
            "model": self._model_hint,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "temperature": temperature if temperature is not None else self._temperature,
        }

        logger.debug(
            f"TokenHub chat: {len(messages)} messages, "
            f"max_tokens={payload['max_tokens']}"
        )

        result = self._post_json("/v1/chat/completions", payload)

        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMConnectionError(
                f"Unexpected response shape from TokenHub: {result!r}"
            ) from e

    def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Generate a completion for a plain text prompt.

        Wraps the prompt as a single user message and delegates to chat().
        """
        messages = [{"role": "user", "content": prompt}]
        return self.chat(messages, max_tokens=max_tokens, temperature=temperature)

    def get_host_status(self) -> List[Dict[str, Any]]:
        """
        Return status information compatible with the old MultiHostClient.

        Since all routing is internal to TokenHub, we return a single entry
        describing the TokenHub instance itself.
        """
        reachable = self._probe_health()
        return [
            {
                "url": self._url,
                "backend": "tokenhub",
                "model": "(routed by tokenhub)",
                "available": reachable,
            }
        ]

    def list_models(self) -> List[str]:
        """
        Return the list of models known to the TokenHub engine.

        Calls GET /admin/v1/engine/models (requires api_key with admin access)
        or falls back to GET /v1/models if that fails.
        Returns ["(tokenhub-routed)"] if neither endpoint is accessible.
        """
        # Try admin engine models endpoint first
        data = self._get_json("/admin/v1/engine/models", timeout=10.0)
        if data:
            models = data.get("models") or data.get("data") or []
            if models:
                ids = [m.get("id") or m.get("name") or str(m) for m in models]
                return [i for i in ids if i]

        # Fall back to OpenAI-compatible /v1/models
        data = self._get_json("/v1/models", timeout=10.0)
        if data:
            models = data.get("data") or []
            ids = [m.get("id") or str(m) for m in models]
            ids = [i for i in ids if i]
            if ids:
                return ids

        return ["(tokenhub-routed)"]

    def shutdown(self) -> None:
        """Release HTTP connection pool. No-op if pooling is not in use."""
        try:
            self._http.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Health probe
    # ------------------------------------------------------------------

    def _probe_health(self, timeout: float = 5.0) -> bool:
        url = f"{self._url}/healthz"
        try:
            if _HTTP_CLIENT_AVAILABLE:
                self._http.request(url=url, method="GET", timeout=timeout)
            else:
                self._http.json_request(url=url, method="GET", timeout=timeout)
            return True
        except Exception:
            return False

    def _probe_auth(self, timeout: float = 10.0) -> None:
        """
        Verify the api_key is accepted by TokenHub.

        Calls GET /v1/models (requires auth) and raises LLMConnectionError
        immediately on 401 so misconfigured keys fail at startup rather than
        on the first real request.  Skipped when no api_key is configured.
        """
        if not self._api_key:
            return
        url = f"{self._url}/v1/models"
        try:
            if _HTTP_CLIENT_AVAILABLE:
                self._http.request(
                    url=url, method="GET", headers=self._auth_headers(), timeout=timeout
                )
            else:
                self._http.json_request(
                    url=url, method="GET", headers=self._auth_headers(), timeout=timeout
                )
        except HTTPError as e:
            if e.code == 401:
                raise LLMConnectionError(
                    f"TokenHub rejected the api_key (401 Unauthorized).\n"
                    f"  • Key must be in tokenhub_<hex> format\n"
                    f"  • Create or rotate a key via the admin UI: {self._url}/admin\n"
                    f"  • Update tokenhub.api_key in config.yaml or TOKENHUB_API_KEY env var"
                ) from e
            # Non-auth errors (404, 5xx) are tolerated; the service is reachable


# ---------------------------------------------------------------------------
# Factory function (same name and signature as old llm_client.create_client_from_config)
# ---------------------------------------------------------------------------

def create_client_from_config(config_dict: Dict[str, Any]) -> TokenHubClient:
    """
    Create a TokenHubClient from a configuration dictionary.

    Reads the 'tokenhub' section of config_dict.  Environment variables
    TOKENHUB_URL and TOKENHUB_API_KEY override the config file values.

    Args:
        config_dict: Full parsed config.yaml as a dict.

    Returns:
        A configured, health-checked TokenHubClient.

    Raises:
        LLMConnectionError: If TokenHub is not reachable at startup.
    """
    th_cfg = config_dict.get("tokenhub") or {}
    perf_cfg = (config_dict.get("review") or {}).get("performance") or {}

    # URL and API key (env vars take priority over config file)
    url = (
        os.environ.get("TOKENHUB_URL")
        or str(th_cfg.get("url") or "http://localhost:8090")
    ).rstrip("/")

    api_key = (
        os.environ.get("TOKENHUB_API_KEY")
        or str(th_cfg.get("api_key") or "")
    )

    model_hint = str(th_cfg.get("model_hint") or "")

    # Request parameters — all in tokenhub: section
    timeout = int(th_cfg.get("timeout") or 600)
    max_tokens = int(th_cfg.get("max_tokens") or 4096)
    temperature = float(
        th_cfg.get("temperature") if th_cfg.get("temperature") is not None
        else 0.1
    )

    max_http_connections = int(perf_cfg.get("max_http_connections") or 16)

    client = TokenHubClient(
        url=url,
        api_key=api_key,
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        model_hint=model_hint,
        max_http_connections=max_http_connections,
    )

    # Verify connectivity at startup — fail fast with a clear message
    if not client._probe_health():
        raise LLMConnectionError(
            f"Cannot reach TokenHub at {url}/healthz\n"
            f"  • Is TokenHub running?  Try: make tokenhub-start\n"
            f"  • Wrong URL?  Update tokenhub.url in config.yaml\n"
            f"  • Run 'make config-init' to reconfigure"
        )

    # Verify the API key is accepted before going any further.
    client._probe_auth()

    # TokenHub's /v1/chat/completions requires a model field.
    # If no model_hint is configured, auto-discover available models.
    if not client._model_hint:
        models = client.list_models()
        real_models = [m for m in models if m != "(tokenhub-routed)"]
        if real_models:
            client._model_hint = real_models[0]
            logger.info(f"Auto-selected model: {client._model_hint}")
        else:
            raise LLMConnectionError(
                f"TokenHub at {url} has no models available.\n"
                f"  • Add providers and models via the admin UI: {url}/admin\n"
                f"  • Or set tokenhub.model_hint in config.yaml"
            )

    logger.info(f"TokenHub connection verified: {url} (model={client._model_hint})")
    return client
