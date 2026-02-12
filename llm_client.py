#!/usr/bin/env python3
"""
Multi-Host LLM Client for AI Code Reviewer

Provides a unified interface for communicating with multiple LLM servers
(vLLM and Ollama) with round-robin load balancing and model fallback.

Features:
- Multiple hosts for parallel throughput (round-robin)
- Multiple models in priority order (fallback if not found)
- Automatic backend detection (vLLM first, then Ollama)
- Concurrent request limiting across all hosts
"""

import logging
import os
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Protocol

from ollama_client import (
    OllamaClient, OllamaConfig, OllamaError,
    OllamaConnectionError, OllamaModelNotFoundError
)
from vllm_client import (
    VLLMClient, VLLMConfig, VLLMError,
    VLLMConnectionError, VLLMModelNotFoundError
)

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base exception for LLM client errors."""
    pass


class LLMConnectionError(LLMError):
    """Raised when no hosts are reachable."""
    pass


class LLMModelNotFoundError(LLMError):
    """Raised when none of the requested models are available on any host."""
    pass


class LLMClientProtocol(Protocol):
    """Protocol defining the interface for LLM clients."""
    
    def chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str: ...
    
    def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str: ...
    
    def list_models(self) -> List[str]: ...


@dataclass
class HostConfig:
    """Configuration for a single host."""
    url: str
    backend: str  # 'vllm' or 'ollama'
    model: str
    client: Any  # VLLMClient or OllamaClient


@dataclass
class UnhealthyHostInfo:
    """Tracks state of an unhealthy host for recovery attempts."""
    host_config: HostConfig
    failed_at: datetime
    failure_reason: Exception
    retry_count: int = 0
    next_retry_at: datetime = field(default_factory=datetime.now)
    consecutive_failures: int = 0


@dataclass
class MultiHostConfig:
    """Configuration for multi-host LLM client."""
    hosts: List[str]
    models: List[str]
    timeout: int = 300
    max_tokens: int = 4096
    temperature: float = 0.1
    max_parallel_requests: int = 4
    num_batch: Optional[int] = None
    adaptive_batching: bool = False
    adaptive_batch_min_chars: int = 8000
    adaptive_batch_max_chars: int = 60000
    adaptive_batch_min: int = 2
    adaptive_batch_max: int = 8
    extra_options: Dict[str, Any] = field(default_factory=dict)
    ps_monitor_interval: float = 0.0
    health_check_enabled: bool = True
    health_check_interval: int = 30
    health_check_max_interval: int = 300
    health_check_timeout: int = 10


class MultiHostClient:
    """
    Client that distributes requests across multiple LLM hosts.
    
    For each host, it tries vLLM first (OpenAI-compatible API), then falls
    back to Ollama. Models are tried in priority order until one is found.
    
    Hosts are grouped by backend type: vLLM hosts are preferred and tried
    first (round-robin), with Ollama hosts as fallback if all vLLM hosts fail.
    
    Requests are distributed round-robin across available hosts for
    increased throughput when processing multiple files in parallel.
    """
    
    def __init__(self, config: MultiHostConfig):
        """
        Initialize the multi-host client.

        Args:
            config: MultiHostConfig with hosts, models, and options

        Raises:
            LLMConnectionError: If no hosts are reachable
            LLMModelNotFoundError: If no models are available on any host
        """
        self.config = config
        self._vllm_hosts: List[HostConfig] = []  # Preferred hosts (vLLM backend)
        self._ollama_hosts: List[HostConfig] = []  # Fallback hosts (Ollama backend)
        self._unhealthy_hosts: Dict[str, UnhealthyHostInfo] = {}  # key: host.url
        self._health_check_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()
        self._vllm_index = 0
        self._ollama_index = 0
        self._host_lock = threading.Lock()
        self._concurrency_lock = threading.Lock()
        self._pending_limit_reduction = 0
        self._dynamic_parallelism = (config.max_parallel_requests == 0)
        
        # Initialize all hosts, separating by backend type
        self._initialize_hosts()
        
        total_hosts = len(self._vllm_hosts) + len(self._ollama_hosts)
        if total_hosts == 0:
            raise LLMConnectionError(
                f"No reachable hosts found. Tried:\n" +
                "\n".join(f"  - {h}" for h in config.hosts)
            )
        
        # Determine parallelism: dynamic (0) or static (1+)
        if self._dynamic_parallelism:
            # Query hosts for recommended parallelism
            recommended = self._query_recommended_parallelism()
            self._effective_max_parallel = recommended
            self._concurrency_sem = threading.Semaphore(recommended)
            logger.info(
                f"Dynamic parallelism: server recommends {recommended} concurrent requests"
            )
            print(f"*** Dynamic parallelism: {recommended} concurrent LLM requests (from server metrics)")
        else:
            self._effective_max_parallel = config.max_parallel_requests
            self._concurrency_sem = threading.Semaphore(config.max_parallel_requests)
            
            # Check if static value differs significantly from recommendation
            try:
                recommended = self._query_recommended_parallelism()
                if abs(recommended - config.max_parallel_requests) >= 2:
                    print(f"\n*** WARNING: LLM parallelism mismatch")
                    print(f"    Config specifies: {config.max_parallel_requests} concurrent requests")
                    print(f"    Server recommends: {recommended} (based on GPU capacity)")
                    if recommended > config.max_parallel_requests:
                        print(f"    Consider setting max_parallel_requests: 0 for better throughput")
                    else:
                        print(f"    Consider setting max_parallel_requests: 0 to avoid overload")
                    print()
            except Exception:
                pass
        
        logger.info(
            f"MultiHostClient initialized: {len(self._vllm_hosts)} vLLM hosts (preferred), "
            f"{len(self._ollama_hosts)} Ollama hosts (fallback), "
            f"max_parallel={self._effective_max_parallel}"
        )

        # Start health check thread for automatic recovery
        self._start_health_check_thread()

    def _total_hosts(self) -> int:
        return len(self._vllm_hosts) + len(self._ollama_hosts)

    def _set_concurrency_limit(self, new_limit: int) -> None:
        new_limit = max(1, int(new_limit))
        with self._concurrency_lock:
            if new_limit == self._effective_max_parallel:
                return

            old_limit = self._effective_max_parallel
            if new_limit < old_limit:
                reduction = old_limit - new_limit
                # Drain available permits to reduce capacity immediately.
                # Only drain permits that are currently free (non-blocking).
                drained = 0
                for _ in range(reduction):
                    acquired = self._concurrency_sem.acquire(blocking=False)
                    if acquired:
                        drained += 1
                    else:
                        break
                # Any remaining reduction is deferred to future releases,
                # but never defer more than (new_limit - 1) to guarantee
                # at least 1 permit can always be acquired.
                remaining = reduction - drained
                safe_max_pending = max(0, new_limit - 1)
                self._pending_limit_reduction = min(
                    self._pending_limit_reduction + remaining, safe_max_pending
                )
            else:
                increase = new_limit - old_limit
                # If we're reducing capacity later, offset that first.
                if self._pending_limit_reduction > 0:
                    offset = min(increase, self._pending_limit_reduction)
                    self._pending_limit_reduction -= offset
                    increase -= offset
                for _ in range(increase):
                    self._concurrency_sem.release()
            self._effective_max_parallel = new_limit
            logger.info(
                f"Concurrency limit changed: {old_limit} -> {new_limit} "
                f"(pending_reduction={self._pending_limit_reduction})"
            )

    def _release_slot(self) -> None:
        with self._concurrency_lock:
            if self._pending_limit_reduction > 0:
                self._pending_limit_reduction -= 1
                return
        self._concurrency_sem.release()

    def _is_host_failure(self, exc: Exception) -> bool:
        if isinstance(exc, (OllamaConnectionError, VLLMConnectionError, TimeoutError, ConnectionError)):
            return True
        message = str(exc).lower()
        indicators = [
            'timed out', 'timeout', 'unreachable', 'connection refused',
            'connection reset', 'cannot connect', 'failed to connect'
        ]
        return any(token in message for token in indicators)

    def _renegotiate_host_model(self, host: HostConfig, exc: Exception) -> Optional[str]:
        """
        Renegotiate model with a host after a model-not-found error.

        When a vLLM worker restarts with a different model, the cached model
        name becomes stale. This queries the server for available models and
        switches to the first one found.

        Args:
            host: The host that returned model-not-found
            exc: The original exception

        Returns:
            New model name if renegotiation succeeded, None otherwise
        """
        if host.backend != 'vllm':
            logger.debug(f"Model renegotiation not supported for {host.backend} backend")
            return None

        try:
            new_model = host.client.renegotiate_model()
            host.model = new_model
            logger.warning(
                f"Host {host.url} model renegotiated to '{new_model}' "
                f"(was: {exc})"
            )
            return new_model
        except Exception as e:
            logger.warning(
                f"Model renegotiation failed for {host.url}: {e}"
            )
            return None

    def _mark_host_unhealthy(self, host: HostConfig, reason: Exception) -> None:
        with self._host_lock:
            before = self._total_hosts()
            # Remove from active pools
            if host.backend == 'vllm':
                self._vllm_hosts = [h for h in self._vllm_hosts if h is not host]
                if self._vllm_index >= len(self._vllm_hosts):
                    self._vllm_index = 0
            else:
                self._ollama_hosts = [h for h in self._ollama_hosts if h is not host]
                if self._ollama_index >= len(self._ollama_hosts):
                    self._ollama_index = 0

            # Move to unhealthy pool for recovery attempts
            now = datetime.now()
            base_interval = self.config.health_check_interval
            jitter = random.uniform(0, base_interval * 0.1)
            next_retry = now + timedelta(seconds=base_interval + jitter)

            if host.url in self._unhealthy_hosts:
                # Update existing entry
                info = self._unhealthy_hosts[host.url]
                info.retry_count += 1
                info.consecutive_failures += 1
                info.failure_reason = reason
                info.next_retry_at = next_retry
            else:
                # Create new entry
                self._unhealthy_hosts[host.url] = UnhealthyHostInfo(
                    host_config=host,
                    failed_at=now,
                    failure_reason=reason,
                    retry_count=0,
                    next_retry_at=next_retry,
                    consecutive_failures=1
                )

            after = self._total_hosts()

        logger.warning(
            f"Host {host.url} marked unhealthy ({host.backend}): {reason}. "
            f"Next retry in {base_interval + jitter:.0f}s. Remaining hosts: {after}"
        )

        if after == 0:
            return
        if before != after:
            if self._dynamic_parallelism:
                new_limit = self._query_recommended_parallelism()
            else:
                ratio = after / max(1, before)
                new_limit = max(1, int(round(self._effective_max_parallel * ratio)))
            self._set_concurrency_limit(new_limit)
    
    def _query_recommended_parallelism(self) -> int:
        """
        Query all hosts to determine recommended parallelism.
        
        Called during initialization to set dynamic parallelism.
        
        Returns:
            Recommended number of concurrent requests (minimum 2)
        """
        total_capacity = 0
        
        # Query vLLM hosts
        for host in self._vllm_hosts:
            try:
                metrics = host.client.get_server_metrics()
                capacity = metrics.get('available_capacity', 1)
                total_capacity += capacity
                logger.debug(f"vLLM host {host.url}: capacity={capacity}")
            except Exception as e:
                logger.debug(f"Could not get metrics from {host.url}: {e}")
                total_capacity += 2  # Assume some capacity
        
        # Query Ollama hosts
        for host in self._ollama_hosts:
            try:
                metrics = host.client.get_server_metrics()
                capacity = metrics.get('available_capacity', 1)
                total_capacity += capacity
                logger.debug(f"Ollama host {host.url}: capacity={capacity}")
            except Exception as e:
                logger.debug(f"Could not get metrics from {host.url}: {e}")
                total_capacity += 2  # Assume some capacity
        
        # Ensure reasonable bounds
        # Minimum of 2 for any parallelism, max of 16 to avoid overwhelming
        recommended = max(2, min(total_capacity, 16))
        
        logger.info(f"Total recommended parallelism across {len(self._vllm_hosts) + len(self._ollama_hosts)} hosts: {recommended}")
        return recommended
    
    def _initialize_hosts(self) -> None:
        """Initialize connections to all hosts, detecting backend and model."""
        for url in self.config.hosts:
            url = url.rstrip('/')
            
            # Expand URLs without ports to try both vLLM (8000) and Ollama (11434)
            urls_to_try = self._expand_host_url(url)
            
            for try_url in urls_to_try:
                host_config = self._probe_and_connect(try_url)
                if host_config:
                    # Separate hosts by backend type for priority routing
                    if host_config.backend == 'vllm':
                        self._vllm_hosts.append(host_config)
                    else:
                        self._ollama_hosts.append(host_config)
                    logger.info(
                        f"Host {try_url}: {host_config.backend} backend, "
                        f"model={host_config.model}"
                    )
                    # Successfully connected, no need to try other ports on this host
                    break
    
    def _expand_host_url(self, url: str) -> List[str]:
        """
        Expand a host URL to try multiple ports if no port specified.
        
        If the URL has no port (e.g., http://myserver), expands to:
          - http://myserver:8000 (vLLM default, tried first)
          - http://myserver:11434 (Ollama default, tried second)
        
        If the URL already has a port, returns it unchanged.
        
        Args:
            url: Host URL, with or without port
            
        Returns:
            List of URLs to try
        """
        import re
        
        # Check if URL already has a port
        # Match: http(s)://host:port or http(s)://host:port/path
        port_pattern = re.compile(r'^(https?://[^/:]+):(\d+)(.*)$')
        if port_pattern.match(url):
            # URL already has a port, use as-is
            return [url]
        
        # Extract base URL (scheme + host)
        base_pattern = re.compile(r'^(https?://[^/:]+)(.*)$')
        match = base_pattern.match(url)
        if not match:
            # Doesn't look like a valid URL, return as-is
            return [url]
        
        base_host = match.group(1)  # e.g., "http://myserver"
        path = match.group(2)       # e.g., "" or "/v1"
        
        # Expand to both default ports (vLLM first, Ollama second)
        logger.info(f"Expanding {url} to try vLLM (:8000) and Ollama (:11434) ports")
        return [
            f"{base_host}:8000{path}",    # vLLM default port
            f"{base_host}:11434{path}",   # Ollama default port
        ]
    
    def _probe_and_connect(self, url: str) -> Optional[HostConfig]:
        """
        Probe a host and establish connection with best available model.
        
        Tries vLLM first, then Ollama. For each backend, tries models
        in priority order.
        
        Args:
            url: Base URL of the host
            
        Returns:
            HostConfig if successful, None if host is unusable
        """
        # Try vLLM first
        if VLLMClient.probe_server(url, timeout=5):
            logger.info(f"Detected vLLM server at {url}")
            client = self._connect_vllm(url)
            if client:
                return client
        
        # Try Ollama
        logger.info(f"Trying Ollama at {url}")
        client = self._connect_ollama(url)
        if client:
            return client
        
        logger.warning(f"Host {url} is not reachable or has no usable models")
        return None
    
    def _connect_vllm(self, url: str) -> Optional[HostConfig]:
        """Try to connect to vLLM with any of the configured models."""
        for model in self.config.models:
            try:
                vllm_config = VLLMConfig(
                    url=url,
                    model=model,
                    timeout=self.config.timeout,
                    max_tokens=self.config.max_tokens,
                    temperature=self.config.temperature,
                    extra_options=self.config.extra_options,
                )
                client = VLLMClient(vllm_config)
                return HostConfig(
                    url=url,
                    backend='vllm',
                    model=model,
                    client=client
                )
            except VLLMModelNotFoundError:
                logger.debug(f"Model {model} not found on vLLM at {url}")
                continue
            except (VLLMError, TimeoutError, ConnectionError) as e:
                logger.debug(f"vLLM error for {model} at {url}: {e}")
                continue
        return None
    
    def _connect_ollama(self, url: str) -> Optional[HostConfig]:
        """Try to connect to Ollama with any of the configured models."""
        for model in self.config.models:
            try:
                ollama_config = OllamaConfig(
                    url=url,
                    model=model,
                    timeout=self.config.timeout,
                    max_tokens=self.config.max_tokens,
                    temperature=self.config.temperature,
                    num_batch=self.config.num_batch,
                    adaptive_batching=self.config.adaptive_batching,
                    adaptive_batch_min_chars=self.config.adaptive_batch_min_chars,
                    adaptive_batch_max_chars=self.config.adaptive_batch_max_chars,
                    adaptive_batch_min=self.config.adaptive_batch_min,
                    adaptive_batch_max=self.config.adaptive_batch_max,
                    extra_options=self.config.extra_options,
                    ps_monitor_interval=self.config.ps_monitor_interval,
                    max_parallel_requests=1,  # Managed at MultiHostClient level
                )
                client = OllamaClient(ollama_config)
                return HostConfig(
                    url=url,
                    backend='ollama',
                    model=model,
                    client=client
                )
            except OllamaModelNotFoundError:
                logger.debug(f"Model {model} not found on Ollama at {url}")
                continue
            except (OllamaError, TimeoutError, ConnectionError) as e:
                logger.debug(f"Ollama error for {model} at {url}: {e}")
                continue
        return None
    
    def _get_next_host(self) -> HostConfig:
        """
        Get the next host in round-robin order.
        
        vLLM hosts are preferred and tried first. Only falls back to
        Ollama hosts if no vLLM hosts are available.
        """
        with self._host_lock:
            if not self._vllm_hosts and not self._ollama_hosts:
                raise LLMConnectionError("No reachable hosts remain")
            # Prefer vLLM hosts
            if self._vllm_hosts:
                host = self._vllm_hosts[self._vllm_index]
                self._vllm_index = (self._vllm_index + 1) % len(self._vllm_hosts)
                return host
            # Fall back to Ollama hosts
            host = self._ollama_hosts[self._ollama_index]
            self._ollama_index = (self._ollama_index + 1) % len(self._ollama_hosts)
            return host
    
    def chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """
        Send a chat completion request to the next available host.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            
        Returns:
            Assistant's response text
        """
        acquired = self._concurrency_sem.acquire(timeout=self.config.timeout)
        if not acquired:
            raise LLMConnectionError(
                f"Timed out waiting for available slot "
                f"(limit={self._effective_max_parallel}, "
                f"pending_reduction={self._pending_limit_reduction})"
            )
        
        try:
            last_error: Optional[Exception] = None
            attempts = 0
            max_attempts = max(1, self._total_hosts())
            while attempts < max_attempts:
                host = self._get_next_host()
                attempts += 1
                try:
                    logger.info(f"Sending chat request to {host.url} ({host.backend}, model={host.model})")
                    return host.client.chat(messages, max_tokens, temperature)
                except VLLMModelNotFoundError as exc:
                    new_model = self._renegotiate_host_model(host, exc)
                    if new_model:
                        # Retry immediately with the new model
                        try:
                            return host.client.chat(messages, max_tokens, temperature)
                        except Exception as retry_exc:
                            last_error = retry_exc
                            if self._is_host_failure(retry_exc):
                                self._mark_host_unhealthy(host, retry_exc)
                            continue
                    last_error = exc
                    continue
                except Exception as exc:
                    if self._is_host_failure(exc):
                        last_error = exc
                        self._mark_host_unhealthy(host, exc)
                        if self._total_hosts() == 0:
                            break
                        continue
                    raise
            raise LLMConnectionError("All hosts failed while handling chat request") from last_error
        finally:
            self._release_slot()

    def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """
        Generate a response from the next available host.
        
        Args:
            prompt: Input prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            
        Returns:
            Generated text
        """
        acquired = self._concurrency_sem.acquire(timeout=self.config.timeout)
        if not acquired:
            raise LLMConnectionError(
                f"Timed out waiting for available slot "
                f"({self._effective_max_parallel} max parallel requests)"
            )
        
        try:
            last_error: Optional[Exception] = None
            attempts = 0
            max_attempts = max(1, self._total_hosts())
            while attempts < max_attempts:
                host = self._get_next_host()
                attempts += 1
                try:
                    logger.debug(f"Routing generate request to {host.url} ({host.backend})")
                    return host.client.generate(prompt, max_tokens, temperature)
                except VLLMModelNotFoundError as exc:
                    new_model = self._renegotiate_host_model(host, exc)
                    if new_model:
                        # Retry immediately with the new model
                        try:
                            return host.client.generate(prompt, max_tokens, temperature)
                        except Exception as retry_exc:
                            last_error = retry_exc
                            if self._is_host_failure(retry_exc):
                                self._mark_host_unhealthy(host, retry_exc)
                            continue
                    last_error = exc
                    continue
                except Exception as exc:
                    if self._is_host_failure(exc):
                        last_error = exc
                        self._mark_host_unhealthy(host, exc)
                        if self._total_hosts() == 0:
                            break
                        continue
                    raise
            raise LLMConnectionError("All hosts failed while handling generate request") from last_error
        finally:
            self._release_slot()
    
    def list_models(self) -> List[str]:
        """List all models available across all hosts."""
        models = set()
        for host in self._vllm_hosts + self._ollama_hosts:
            try:
                models.update(host.client.list_models())
            except Exception as e:
                logger.warning(f"Failed to list models from {host.url}: {e}")
        return sorted(models)
    
    def get_host_status(self) -> List[Dict[str, Any]]:
        """Get status information for all configured hosts (vLLM first, then Ollama)."""
        status = []
        for host in self._vllm_hosts + self._ollama_hosts:
            status.append({
                'url': host.url,
                'backend': host.backend,
                'model': host.model,
                'available': True,
            })
        return status
    
    def get_server_metrics(self) -> Dict[str, Any]:
        """
        Get aggregated server metrics from all hosts.
        
        Returns combined metrics from vLLM and Ollama backends.
        """
        metrics = {
            'total_capacity': 0,
            'kv_cache_usage': 0.0,
            'requests_running': 0,
            'requests_waiting': 0,
            'hosts': []
        }
        
        # Prefer vLLM hosts for metrics (more detailed)
        for host in self._vllm_hosts:
            try:
                host_metrics = host.client.get_server_metrics()
                metrics['hosts'].append({
                    'url': host.url,
                    'backend': 'vllm',
                    **host_metrics
                })
                metrics['total_capacity'] += host_metrics.get('available_capacity', 1)
                metrics['kv_cache_usage'] = max(
                    metrics['kv_cache_usage'], 
                    host_metrics.get('kv_cache_usage', 0)
                )
                metrics['requests_running'] += host_metrics.get('requests_running', 0)
                metrics['requests_waiting'] += host_metrics.get('requests_waiting', 0)
            except Exception as e:
                logger.debug(f"Failed to get metrics from vLLM host {host.url}: {e}")
        
        # Also check Ollama hosts
        for host in self._ollama_hosts:
            try:
                host_metrics = host.client.get_server_metrics()
                metrics['hosts'].append({
                    'url': host.url,
                    'backend': 'ollama',
                    **host_metrics
                })
                metrics['total_capacity'] += host_metrics.get('available_capacity', 1)
            except Exception as e:
                logger.debug(f"Failed to get metrics from Ollama host {host.url}: {e}")
        
        # Ensure at least 1 capacity
        metrics['total_capacity'] = max(1, metrics['total_capacity'])
        
        return metrics
    
    def get_recommended_parallelism(self, max_parallel: int = 16) -> int:
        """
        Get recommended number of parallel requests based on server metrics.
        
        Queries all configured hosts and returns a recommendation based on
        their combined capacity.
        
        Args:
            max_parallel: Maximum parallelism cap from config
            
        Returns:
            Recommended number of parallel requests (1 to max_parallel)
        """
        metrics = self.get_server_metrics()
        num_hosts = len(self._vllm_hosts) + len(self._ollama_hosts)
        
        # Use total capacity across all hosts
        recommended = metrics['total_capacity']
        
        # If there are many waiting requests across hosts, reduce parallelism
        if metrics['requests_waiting'] > num_hosts * 2:
            recommended = max(num_hosts, recommended - metrics['requests_waiting'])
        
        # Cap at max_parallel
        recommended = max(1, min(recommended, max_parallel))
        
        # Show per-host breakdown for diagnostics
        host_info = []
        for h in metrics.get('hosts', []):
            host_info.append(f"{h.get('url', '?')}: KV={h.get('kv_cache_usage', 0):.1%}")
        
        logger.info(
            f"Multi-host parallelism: {recommended} "
            f"(total_capacity={metrics['total_capacity']}, hosts={num_hosts})"
        )
        if host_info:
            logger.info(f"  Per-host: {', '.join(host_info)}")

        return recommended

    def _update_retry_schedule(self, info: UnhealthyHostInfo, reason: Optional[Exception] = None) -> None:
        """Update retry schedule with exponential backoff."""
        info.retry_count += 1
        info.consecutive_failures += 1
        if reason:
            info.failure_reason = reason

        base_interval = self.config.health_check_interval
        max_interval = self.config.health_check_max_interval

        # Exponential backoff: base * (2 ** retry_count) with jitter
        interval = min(base_interval * (2 ** info.retry_count), max_interval)
        jitter = random.uniform(0, interval * 0.1)
        interval += jitter

        info.next_retry_at = datetime.now() + timedelta(seconds=interval)
        logger.debug(
            f"Reconnection to {info.host_config.url} failed (retry #{info.retry_count}): "
            f"{reason or info.failure_reason}. Next retry in {interval:.0f}s"
        )

    def _validate_host_connection(self, host: HostConfig) -> bool:
        """
        Validate a host connection for health check recovery.

        Uses a short timeout to avoid blocking the health check thread
        for minutes when a host is unreachable (e.g., DNS/mDNS for .local
        addresses can block far longer than TCP connect timeouts).

        Returns:
            True if connection is valid, False otherwise
        """
        import concurrent.futures

        timeout = self.config.health_check_timeout

        def _do_validate():
            if host.backend == 'vllm':
                host.client._validate_connection()
            else:
                host.client._validate_connection()

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_do_validate)
                future.result(timeout=timeout)
            return True
        except concurrent.futures.TimeoutError:
            logger.debug(
                f"Health check timed out for {host.url} after {timeout}s"
            )
            return False
        except Exception as e:
            logger.debug(f"Health check failed for {host.url}: {e}")
            return False

    def _restore_healthy_host(self, info: UnhealthyHostInfo) -> None:
        """Restore a recovered host to the active pool."""
        with self._host_lock:
            host = info.host_config
            # Remove from unhealthy pool
            if host.url in self._unhealthy_hosts:
                del self._unhealthy_hosts[host.url]

            # Add back to appropriate active pool
            if host.backend == 'vllm':
                self._vllm_hosts.append(host)
            else:
                self._ollama_hosts.append(host)

            after = self._total_hosts()

        downtime = datetime.now() - info.failed_at
        downtime_str = str(downtime).split('.')[0]  # Remove microseconds

        logger.info(
            f"Host {host.url} recovered after {info.retry_count} retries (down {downtime_str}). "
            f"Restored to active pool. Active hosts: {after}"
        )

        # Recalculate parallelism if dynamic mode
        if self._dynamic_parallelism:
            try:
                new_limit = self._query_recommended_parallelism()
                self._set_concurrency_limit(new_limit)
            except Exception as e:
                logger.debug(f"Could not recalculate parallelism after recovery: {e}")

    def _attempt_host_recovery(self) -> None:
        """Attempt to recover hosts that are due for retry."""
        now = datetime.now()

        # Find hosts due for retry (snapshot to avoid holding lock during I/O)
        with self._host_lock:
            hosts_to_retry = [
                (url, info) for url, info in self._unhealthy_hosts.items()
                if info.next_retry_at <= now
            ]

        # Try recovery for each due host
        for url, info in hosts_to_retry:
            logger.info(f"Attempting reconnection to {url} (retry #{info.retry_count + 1})...")

            if self._validate_host_connection(info.host_config):
                # Success - restore to active pool
                self._restore_healthy_host(info)
            else:
                # Failed - update retry schedule
                self._update_retry_schedule(info)

    def _health_check_loop(self) -> None:
        """Background thread loop for health checks."""
        logger.info("Health check thread started")
        last_status_log = datetime.now()

        while not self._shutdown_event.is_set():
            # Wake every 5 seconds to check for work
            self._shutdown_event.wait(timeout=5.0)

            if self._shutdown_event.is_set():
                break

            # Attempt recovery for any due hosts
            try:
                self._attempt_host_recovery()
            except Exception as e:
                logger.error(f"Error in health check loop: {e}", exc_info=True)

            # Log status more frequently when hosts are unhealthy
            with self._host_lock:
                healthy = len(self._vllm_hosts) + len(self._ollama_hosts)
                unhealthy = len(self._unhealthy_hosts)
                unhealthy_urls = list(self._unhealthy_hosts.keys())
            interval = timedelta(minutes=1) if unhealthy > 0 else timedelta(minutes=5)
            if datetime.now() - last_status_log >= interval:
                if healthy > 0 or unhealthy > 0:
                    msg = f"Connection pool: {healthy} healthy, {unhealthy} unhealthy"
                    if unhealthy_urls:
                        msg += f" ({', '.join(unhealthy_urls)})"
                    msg += f" | concurrency={self._effective_max_parallel}"
                    logger.info(msg)
                last_status_log = datetime.now()

        logger.info("Health check thread stopped")

    def _start_health_check_thread(self) -> None:
        """Start the background health check thread."""
        if not self.config.health_check_enabled:
            logger.info("Health check disabled by configuration")
            return

        self._health_check_thread = threading.Thread(
            target=self._health_check_loop,
            daemon=True,
            name="LLM-HealthCheck"
        )
        self._health_check_thread.start()
        logger.info("Health check thread initialized")

    def shutdown(self) -> None:
        """Shutdown the client and health check thread."""
        logger.info("Shutting down MultiHostClient...")
        self._shutdown_event.set()

        if self._health_check_thread and self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=10)
            if self._health_check_thread.is_alive():
                logger.warning("Health check thread did not stop gracefully")
            else:
                logger.info("Health check thread stopped")

        logger.info("MultiHostClient shutdown complete")


def create_client_from_config(config_dict: Dict[str, Any]) -> MultiHostClient:
    """
    Create a MultiHostClient from a configuration dictionary.
    
    Supports both new 'llm' config section and legacy 'ollama' section
    for backward compatibility.
    
    Args:
        config_dict: Dictionary with 'llm' or 'ollama' section
        
    Returns:
        Configured MultiHostClient
    """
    # Check for new 'llm' config section first
    llm_config = config_dict.get('llm', {})
    
    # Fall back to 'ollama' section for backward compatibility
    if not llm_config:
        ollama_config = config_dict.get('ollama', {})
        if ollama_config:
            logger.info("Using legacy 'ollama' config section")
            llm_config = _convert_legacy_config(ollama_config)
    
    # Environment variable overrides
    env_hosts = os.environ.get('ANGRY_AI_LLM_HOSTS') or os.environ.get('ANGRY_AI_OLLAMA_URL') or os.environ.get('OLLAMA_URL')
    env_models = os.environ.get('ANGRY_AI_LLM_MODELS') or os.environ.get('ANGRY_AI_OLLAMA_MODEL') or os.environ.get('OLLAMA_MODEL')
    env_parallel = os.environ.get('ANGRY_AI_LLM_MAX_PARALLEL') or os.environ.get('ANGRY_AI_OLLAMA_MAX_PARALLEL')
    
    # Parse hosts (comma-separated string or list)
    hosts = llm_config.get('hosts', [])
    if env_hosts:
        hosts = [h.strip() for h in env_hosts.split(',')]
    elif isinstance(hosts, str):
        hosts = [hosts]
    elif not hosts:
        # Default to localhost (port auto-expansion will try :8000 then :11434)
        hosts = ['http://localhost']
    
    # Parse models (comma-separated string or list)
    models = llm_config.get('models', [])
    if env_models:
        models = [m.strip() for m in env_models.split(',')]
    elif isinstance(models, str):
        models = [models]
    elif not models:
        # Default models in priority order (must match config.yaml.defaults)
        models = ['nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16', 'qwen2.5-coder:32b']
    
    # Parse batching config
    batching_cfg = llm_config.get('batching', {})
    if not isinstance(batching_cfg, dict):
        batching_cfg = {}
    
    def _coerce_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    
    def _coerce_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return default
    
    # Determine num_batch
    env_num_batch = os.environ.get('ANGRY_AI_LLM_NUM_BATCH') or os.environ.get('ANGRY_AI_OLLAMA_NUM_BATCH')
    if env_num_batch is not None:
        try:
            num_batch = int(env_num_batch)
        except ValueError:
            num_batch = None
    else:
        raw_num_batch = batching_cfg.get('num_batch')
        if raw_num_batch is None:
            num_batch = None
        else:
            try:
                num_batch = int(raw_num_batch)
            except (TypeError, ValueError):
                num_batch = None
    
    # Max parallel requests (0 = dynamic from server metrics)
    if env_parallel:
        try:
            max_parallel = int(env_parallel)
        except ValueError:
            max_parallel = _coerce_int(batching_cfg.get('max_parallel_requests', 0), 0)
    else:
        max_parallel = _coerce_int(batching_cfg.get('max_parallel_requests', 0), 0)
    
    extra_options = llm_config.get('options', {})
    if not isinstance(extra_options, dict):
        extra_options = {}

    # Parse health check config
    health_check_cfg = llm_config.get('health_check', {})
    if not isinstance(health_check_cfg, dict):
        health_check_cfg = {}

    config = MultiHostConfig(
        hosts=hosts,
        models=models,
        timeout=llm_config.get('timeout', 300),
        max_tokens=llm_config.get('max_tokens', 4096),
        temperature=llm_config.get('temperature', 0.1),
        max_parallel_requests=max_parallel,  # 0 = dynamic, 1+ = static
        num_batch=num_batch,
        adaptive_batching=_coerce_bool(batching_cfg.get('adaptive'), False) if num_batch is None else False,
        adaptive_batch_min_chars=_coerce_int(batching_cfg.get('min_chars', 8000), 8000),
        adaptive_batch_max_chars=_coerce_int(batching_cfg.get('max_chars', 60000), 60000),
        adaptive_batch_min=_coerce_int(batching_cfg.get('min_num_batch', 2), 2),
        adaptive_batch_max=_coerce_int(batching_cfg.get('max_num_batch', 8), 8),
        extra_options=extra_options,
        ps_monitor_interval=max(0.0, float(llm_config.get('ps_monitor_interval', 0))),
        health_check_enabled=_coerce_bool(health_check_cfg.get('enabled', True), True),
        health_check_interval=_coerce_int(health_check_cfg.get('interval', 30), 30),
        health_check_max_interval=_coerce_int(health_check_cfg.get('max_interval', 300), 300),
        health_check_timeout=_coerce_int(health_check_cfg.get('timeout', 10), 10),
    )
    
    return MultiHostClient(config)


def _convert_legacy_config(ollama_config: Dict[str, Any]) -> Dict[str, Any]:
    """Convert legacy 'ollama' config section to new 'llm' format."""
    import re
    
    # Extract just the host without port - port auto-expansion handles the rest
    old_url = ollama_config.get('url', 'http://localhost:11434')
    url_match = re.match(r'(https?://[^:/]+)(:\d+)?(/.*)?', old_url)
    if url_match:
        base_host = url_match.group(1)
        # Just use base host - _expand_host_url will try :8000 then :11434
        hosts = [base_host]
    else:
        hosts = [old_url]
    
    return {
        'hosts': hosts,
        'models': ['nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16', ollama_config.get('model', 'qwen2.5-coder:32b')],
        'timeout': ollama_config.get('timeout', 300),
        'max_tokens': ollama_config.get('max_tokens', 4096),
        'temperature': ollama_config.get('temperature', 0.1),
        'ps_monitor_interval': ollama_config.get('ps_monitor_interval', 0),
        'batching': ollama_config.get('batching', {}),
        'options': ollama_config.get('options', {}),
    }


# Re-export errors for convenience
__all__ = [
    'MultiHostClient',
    'MultiHostConfig',
    'LLMError',
    'LLMConnectionError',
    'LLMModelNotFoundError',
    'create_client_from_config',
]
