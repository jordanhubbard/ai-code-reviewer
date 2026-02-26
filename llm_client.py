#!/usr/bin/env python3
"""
LLM Client — OpenAI-compatible multi-provider client for AI Code Reviewer

Supports one or more LLM providers (vLLM, TokenHub, OpenAI, Ollama with
OpenAI-compat layer, etc.) configured as URL/key tuples.  Providers are
tried in order; the first healthy one wins.

Public interface:
    - chat(messages, ...)       -> str
    - generate(prompt, ...)     -> str
    - get_host_status()         -> List[Dict]
    - list_models()             -> List[str]
    - get_recommended_parallelism() -> int
    - shutdown()                -> None

Configuration (config.yaml):
    llm:
      providers:
        - url: "http://localhost:8090"
          api_key: ""           # optional Bearer token
        - url: "http://my-vllm:8000"
          api_key: "sk-..."
      model: ""                 # blank = auto-discover first available
      timeout: 600
      max_tokens: 4096
      temperature: 0.1

Environment variable overrides (applied to *first* provider only):
    LLM_URL           – override first provider URL
    LLM_API_KEY       – override first provider API key
    TOKENHUB_URL      – legacy alias for LLM_URL
    TOKENHUB_API_KEY  – legacy alias for LLM_API_KEY
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Base exception for LLM errors."""

class LLMConnectionError(LLMError):
    """Raised when no provider is reachable."""

class LLMModelNotFoundError(LLMError):
    """Raised when no model is available for the request."""


# ---------------------------------------------------------------------------
# HTTP helpers
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
    """Minimal HTTP helper when async_http_client is unavailable."""

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
# Provider descriptor
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    url: str
    api_key: str = ""


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class LLMClient:
    """
    OpenAI-compatible LLM client with multi-provider failover.

    Providers are tried in order.  The first one to successfully respond
    to a health/model check at startup is used for all subsequent requests.
    If a request fails, the client attempts the remaining providers.
    """

    def __init__(
        self,
        providers: List[ProviderConfig],
        timeout: int = 600,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        model: str = "",
        max_http_connections: int = 16,
    ):
        if not providers:
            raise LLMConnectionError("No LLM providers configured")

        self._providers = providers
        self._active_idx = 0
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._model = model

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

        names = ", ".join(p.url for p in providers)
        logger.info(f"LLMClient initialised with {len(providers)} provider(s): {names}")

    # Expose timeout for callers that reference client.config.timeout
    @property
    def config(self):
        return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_headers(self, provider: ProviderConfig) -> Dict[str, str]:
        if provider.api_key:
            return {"Authorization": f"Bearer {provider.api_key}"}
        return {}

    def _post_json(
        self, provider: ProviderConfig, path: str, payload: Dict,
        timeout: Optional[float] = None,
    ) -> Dict:
        url = f"{provider.url}{path}"
        hdrs = self._auth_headers(provider)
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
                    headers=self._auth_headers(provider), timeout=t,
                )
        except HTTPError as e:
            body_text = getattr(e, "response_body", None)
            detail = body_text.decode(errors="replace") if body_text else str(e)
            if e.code == 401:
                raise LLMConnectionError(
                    f"Provider {provider.url} returned 401 Unauthorized — check api_key: {detail}"
                ) from e
            if e.code == 404:
                raise LLMModelNotFoundError(
                    f"Provider {provider.url} returned 404 — model not found: {detail}"
                ) from e
            raise LLMConnectionError(
                f"Provider HTTP {e.code} at {url}: {detail}"
            ) from e
        except URLError as e:
            raise LLMConnectionError(
                f"Cannot reach provider at {url}: {e}"
            ) from e

    def _get_json(
        self, provider: ProviderConfig, path: str, timeout: float = 10.0,
    ) -> Dict:
        url = f"{provider.url}{path}"
        hdrs = self._auth_headers(provider)
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

    def _probe_provider(self, provider: ProviderConfig, timeout: float = 5.0) -> bool:
        """Check if a provider is reachable via GET /v1/models."""
        url = f"{provider.url}/v1/models"
        try:
            if _HTTP_CLIENT_AVAILABLE:
                self._http.request(
                    url=url, method="GET",
                    headers=self._auth_headers(provider), timeout=timeout,
                )
            else:
                self._http.json_request(
                    url=url, method="GET",
                    headers=self._auth_headers(provider), timeout=timeout,
                )
            return True
        except Exception:
            return False

    def _active_provider(self) -> ProviderConfig:
        return self._providers[self._active_idx]

    def _try_chat_with_failover(self, payload: Dict) -> Dict:
        """Post to /v1/chat/completions, failing over across providers."""
        errors = []
        for offset in range(len(self._providers)):
            idx = (self._active_idx + offset) % len(self._providers)
            provider = self._providers[idx]
            try:
                result = self._post_json(provider, "/v1/chat/completions", payload)
                if idx != self._active_idx:
                    logger.info(f"Failover: switching active provider to {provider.url}")
                    self._active_idx = idx
                return result
            except LLMModelNotFoundError:
                raise
            except LLMError as e:
                errors.append(f"{provider.url}: {e}")
                logger.warning(f"Provider {provider.url} failed: {e}")
                continue

        raise LLMConnectionError(
            "All LLM providers failed:\n  " + "\n  ".join(errors)
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "messages": messages,
            "model": self._model,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "temperature": temperature if temperature is not None else self._temperature,
        }

        logger.debug(
            f"LLM chat: {len(messages)} messages, "
            f"max_tokens={payload['max_tokens']}"
        )

        result = self._try_chat_with_failover(payload)

        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMConnectionError(
                f"Unexpected response shape from LLM provider: {result!r}"
            ) from e

    def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        messages = [{"role": "user", "content": prompt}]
        return self.chat(messages, max_tokens=max_tokens, temperature=temperature)

    def get_host_status(self) -> List[Dict[str, Any]]:
        statuses = []
        for p in self._providers:
            reachable = self._probe_provider(p)
            statuses.append({
                "url": p.url,
                "backend": "openai-compat",
                "model": self._model or "(auto)",
                "available": reachable,
            })
        return statuses

    def list_models(self) -> List[str]:
        for p in self._providers:
            data = self._get_json(p, "/v1/models", timeout=10.0)
            if data:
                models = data.get("data") or []
                ids = [m.get("id") or str(m) for m in models]
                ids = [i for i in ids if i]
                if ids:
                    return ids
        return ["(unknown)"]

    def get_recommended_parallelism(self, max_parallel: int = 16) -> int:
        """
        Return a sensible parallelism level.

        Generic providers don't expose GPU/KV-cache metrics, so we return
        a conservative default.  Callers should catch exceptions and fall
        back to their own defaults.
        """
        return min(4, max_parallel)

    def shutdown(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_client_from_config(config_dict: Dict[str, Any]) -> LLMClient:
    """
    Create an LLMClient from a configuration dictionary.

    Supports three config layouts (checked in order):
      1. llm.providers: [{url, api_key}, ...]   (new canonical format)
      2. tokenhub: {url, api_key}                (legacy single-provider)
      3. Environment variables only               (minimal config)

    Environment variables LLM_URL / LLM_API_KEY (or legacy TOKENHUB_URL /
    TOKENHUB_API_KEY) override the first provider's settings.
    """
    providers: List[ProviderConfig] = []

    llm_cfg = config_dict.get("llm") or {}
    th_cfg = config_dict.get("tokenhub") or {}
    perf_cfg = (config_dict.get("review") or {}).get("performance") or {}

    # --- Build provider list from config ---

    if "providers" in llm_cfg:
        for entry in llm_cfg["providers"]:
            if isinstance(entry, dict) and entry.get("url"):
                providers.append(ProviderConfig(
                    url=str(entry["url"]).rstrip("/"),
                    api_key=str(entry.get("api_key") or ""),
                ))
    elif th_cfg.get("url"):
        providers.append(ProviderConfig(
            url=str(th_cfg["url"]).rstrip("/"),
            api_key=str(th_cfg.get("api_key") or ""),
        ))

    # --- Environment variable overrides (first provider) ---
    env_url = os.environ.get("LLM_URL") or os.environ.get("TOKENHUB_URL")
    env_key = os.environ.get("LLM_API_KEY") or os.environ.get("TOKENHUB_API_KEY")

    if env_url or env_key:
        if providers:
            if env_url:
                providers[0].url = env_url.rstrip("/")
            if env_key:
                providers[0].api_key = env_key
        elif env_url:
            providers.append(ProviderConfig(
                url=env_url.rstrip("/"),
                api_key=env_key or "",
            ))

    if not providers:
        raise LLMConnectionError(
            "No LLM providers configured.\n"
            "  Add at least one provider in config.yaml under llm.providers:\n"
            "    llm:\n"
            "      providers:\n"
            "        - url: \"http://localhost:8090\"\n"
            "  Or set LLM_URL environment variable."
        )

    # --- Request parameters (llm section takes precedence, fall back to tokenhub) ---
    model = str(
        llm_cfg.get("model")
        or th_cfg.get("model_hint")
        or ""
    )
    timeout = int(llm_cfg.get("timeout") or th_cfg.get("timeout") or 600)
    max_tokens = int(llm_cfg.get("max_tokens") or th_cfg.get("max_tokens") or 4096)
    temperature_raw = llm_cfg.get("temperature")
    if temperature_raw is None:
        temperature_raw = th_cfg.get("temperature")
    temperature = float(temperature_raw) if temperature_raw is not None else 0.1

    max_http_connections = int(perf_cfg.get("max_http_connections") or 16)

    client = LLMClient(
        providers=providers,
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model,
        max_http_connections=max_http_connections,
    )

    # --- Probe providers at startup ---
    reachable = []
    for i, p in enumerate(providers):
        if client._probe_provider(p):
            reachable.append(i)

    if not reachable:
        urls = ", ".join(p.url for p in providers)
        raise LLMConnectionError(
            f"No LLM providers are reachable: {urls}\n"
            "  Check that at least one provider URL is correct and the server is running.\n"
            "  Run 'make validate' to test connectivity."
        )

    client._active_idx = reachable[0]
    active = providers[reachable[0]]
    logger.info(
        f"Active LLM provider: {active.url} "
        f"({len(reachable)}/{len(providers)} reachable)"
    )

    # --- Auto-discover model if not specified ---
    if not client._model:
        models = client.list_models()
        real_models = [m for m in models if not m.startswith("(")]
        if real_models:
            client._model = real_models[0]
            logger.info(f"Auto-selected model: {client._model}")
        else:
            raise LLMConnectionError(
                f"Provider {active.url} has no models available.\n"
                "  Ensure the LLM server has at least one model loaded.\n"
                "  Or set llm.model in config.yaml explicitly."
            )

    logger.info(f"LLM connection verified: {active.url} (model={client._model})")
    return client
