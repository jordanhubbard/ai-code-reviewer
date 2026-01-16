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
import threading
from dataclasses import dataclass, field
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
        self._vllm_index = 0
        self._ollama_index = 0
        self._host_lock = threading.Lock()
        self._concurrency_sem = threading.Semaphore(max(1, config.max_parallel_requests))
        
        # Initialize all hosts, separating by backend type
        self._initialize_hosts()
        
        total_hosts = len(self._vllm_hosts) + len(self._ollama_hosts)
        if total_hosts == 0:
            raise LLMConnectionError(
                f"No reachable hosts found. Tried:\n" +
                "\n".join(f"  - {h}" for h in config.hosts)
            )
        
        logger.info(
            f"MultiHostClient initialized: {len(self._vllm_hosts)} vLLM hosts (preferred), "
            f"{len(self._ollama_hosts)} Ollama hosts (fallback), "
            f"max_parallel={config.max_parallel_requests}"
        )
    
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
            except VLLMError as e:
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
            except OllamaError as e:
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
                f"({self.config.max_parallel_requests} max parallel requests)"
            )
        
        try:
            host = self._get_next_host()
            logger.debug(f"Routing chat request to {host.url} ({host.backend})")
            return host.client.chat(messages, max_tokens, temperature)
        finally:
            self._concurrency_sem.release()
    
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
                f"({self.config.max_parallel_requests} max parallel requests)"
            )
        
        try:
            host = self._get_next_host()
            logger.debug(f"Routing generate request to {host.url} ({host.backend})")
            return host.client.generate(prompt, max_tokens, temperature)
        finally:
            self._concurrency_sem.release()
    
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
    
    # Max parallel requests
    if env_parallel:
        try:
            max_parallel = int(env_parallel)
        except ValueError:
            max_parallel = _coerce_int(batching_cfg.get('max_parallel_requests', 4), 4)
    else:
        max_parallel = _coerce_int(batching_cfg.get('max_parallel_requests', 4), 4)
    
    extra_options = llm_config.get('options', {})
    if not isinstance(extra_options, dict):
        extra_options = {}
    
    config = MultiHostConfig(
        hosts=hosts,
        models=models,
        timeout=llm_config.get('timeout', 300),
        max_tokens=llm_config.get('max_tokens', 4096),
        temperature=llm_config.get('temperature', 0.1),
        max_parallel_requests=max(1, max_parallel),
        num_batch=num_batch,
        adaptive_batching=_coerce_bool(batching_cfg.get('adaptive'), False) if num_batch is None else False,
        adaptive_batch_min_chars=_coerce_int(batching_cfg.get('min_chars', 8000), 8000),
        adaptive_batch_max_chars=_coerce_int(batching_cfg.get('max_chars', 60000), 60000),
        adaptive_batch_min=_coerce_int(batching_cfg.get('min_num_batch', 2), 2),
        adaptive_batch_max=_coerce_int(batching_cfg.get('max_num_batch', 8), 8),
        extra_options=extra_options,
        ps_monitor_interval=max(0.0, float(llm_config.get('ps_monitor_interval', 0))),
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
