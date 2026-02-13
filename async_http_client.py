#!/usr/bin/env python3
"""
HTTP Client with Connection Pooling

Provides a synchronous HTTP client interface with connection pooling
for improved performance. Uses httpx when available, falls back to urllib.

Key features:
- Connection pooling and reuse
- HTTP/2 support
- Configurable connection limits
- Drop-in replacement for urllib
- Synchronous interface (no async/await needed)
"""

import json
import logging
from typing import Dict, Optional, Any
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

# Try to import httpx, fall back to urllib if not available
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    from urllib.request import Request, urlopen


class PooledHTTPClient:
    """
    HTTP client with connection pooling for better performance.

    Uses httpx when available for connection pooling and HTTP/2 support.
    Falls back to urllib (no pooling) if httpx is not installed.
    """

    def __init__(
        self,
        max_connections: int = 16,
        max_keepalive_connections: int = 10,
        timeout: float = 300.0,
    ):
        """
        Initialize the pooled HTTP client.

        Args:
            max_connections: Maximum concurrent connections
            max_keepalive_connections: Maximum connections to keep alive
            timeout: Default timeout in seconds
        """
        self.timeout = timeout
        self._client = None

        if HTTPX_AVAILABLE:
            # Create httpx client with connection pooling
            limits = httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
                keepalive_expiry=30.0,  # Keep connections alive for 30s
            )
            self._client = httpx.Client(
                limits=limits,
                timeout=timeout,
                http2=False,  # HTTP/2 disabled (requires h2 package)
                follow_redirects=True,
            )
            logger.info(
                f"HTTP client initialized with connection pooling "
                f"(max_connections={max_connections}, keepalive={max_keepalive_connections})"
            )
        else:
            logger.warning(
                "httpx not available, falling back to urllib (no connection pooling). "
                "Install httpx for better performance: pip install httpx"
            )

    def __del__(self):
        """Clean up the HTTP client."""
        self.close()

    def close(self):
        """Close the HTTP client and release connections."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:
                logger.debug(f"Error closing HTTP client: {e}")
            self._client = None

    def request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        timeout: Optional[float] = None,
    ) -> tuple[int, Dict[str, str], bytes]:
        """
        Make an HTTP request.

        Args:
            url: URL to request
            method: HTTP method (GET, POST, etc.)
            headers: Optional HTTP headers
            data: Optional request body
            timeout: Optional timeout override

        Returns:
            Tuple of (status_code, response_headers, response_body)

        Raises:
            HTTPError: On HTTP error status
            URLError: On connection error
        """
        timeout = timeout or self.timeout
        headers = headers or {}

        if HTTPX_AVAILABLE and self._client is not None:
            # Use httpx with connection pooling
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=data,
                    timeout=timeout,
                )

                # Convert to format similar to urllib
                response_headers = dict(response.headers)
                response_body = response.content

                # Raise for error status codes (similar to urllib behavior)
                if response.status_code >= 400:
                    # Create an HTTPError-like exception
                    error = HTTPError(
                        url=url,
                        code=response.status_code,
                        msg=response.reason_phrase,
                        hdrs=response_headers,
                        fp=None,
                    )
                    # Attach response body for error details
                    error.response_body = response_body
                    raise error

                return response.status_code, response_headers, response_body

            except httpx.TimeoutException as e:
                raise URLError(f"Request timed out after {timeout}s: {e}") from e
            except httpx.ConnectError as e:
                raise URLError(f"Connection failed: {e}") from e
            except httpx.RequestError as e:
                raise URLError(f"Request failed: {e}") from e

        else:
            # Fall back to urllib (no connection pooling)
            request = Request(url, data=data, headers=headers, method=method)
            try:
                with urlopen(request, timeout=timeout) as response:
                    response_headers = dict(response.headers)
                    response_body = response.read()
                    return response.status, response_headers, response_body
            except HTTPError as e:
                # Attach response body for error details
                e.response_body = e.read() if e.fp else b""
                raise
            except URLError:
                raise

    def json_request(
        self,
        url: str,
        method: str = "GET",
        json_data: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Make a JSON HTTP request.

        Args:
            url: URL to request
            method: HTTP method
            json_data: Optional JSON data to send
            timeout: Optional timeout override

        Returns:
            Parsed JSON response

        Raises:
            HTTPError: On HTTP error status
            URLError: On connection error
            json.JSONDecodeError: On invalid JSON response
        """
        headers = {"Content-Type": "application/json"}
        data = json.dumps(json_data).encode("utf-8") if json_data else None

        status_code, response_headers, response_body = self.request(
            url=url,
            method=method,
            headers=headers,
            data=data,
            timeout=timeout,
        )

        return json.loads(response_body.decode("utf-8"))


# Global client instance (created on first use)
_global_client: Optional[PooledHTTPClient] = None


def get_global_client(
    max_connections: int = 16,
    timeout: float = 300.0,
) -> PooledHTTPClient:
    """
    Get the global pooled HTTP client instance.

    Args:
        max_connections: Maximum concurrent connections
        timeout: Default timeout in seconds

    Returns:
        Shared PooledHTTPClient instance
    """
    global _global_client

    if _global_client is None:
        _global_client = PooledHTTPClient(
            max_connections=max_connections,
            max_keepalive_connections=max(1, max_connections // 2),
            timeout=timeout,
        )

    return _global_client


def close_global_client():
    """Close the global HTTP client."""
    global _global_client
    if _global_client is not None:
        _global_client.close()
        _global_client = None
