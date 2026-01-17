#!/usr/bin/env python3
"""
Ollama Client for Angry AI

Handles communication with a remote Ollama server using the OpenAI-compatible API.
Provides early validation of server connectivity and model availability.

This module is designed to be cross-platform and run on FreeBSD.
"""

import json
import logging
import os
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    """Base exception for Ollama-related errors."""
    pass


class OllamaConnectionError(OllamaError):
    """Raised when connection to Ollama server fails."""
    pass


class OllamaModelNotFoundError(OllamaError):
    """Raised when the requested model is not available on the server."""
    pass


class OllamaValidationError(OllamaError):
    """Raised when the known-answer handshake fails."""
    pass


@dataclass
class OllamaConfig:
    """Configuration for Ollama client."""
    url: str
    model: str
    timeout: int = 300
    max_tokens: int = 4096
    temperature: float = 0.1
    num_batch: Optional[int] = None
    adaptive_batching: bool = False
    adaptive_batch_min_chars: int = 8000
    adaptive_batch_max_chars: int = 60000
    adaptive_batch_min: int = 2
    adaptive_batch_max: int = 8
    extra_options: Dict[str, Any] = field(default_factory=dict)
    ps_monitor_interval: float = 0.0
    max_parallel_requests: int = 1
    num_ctx: Optional[int] = None  # Model's context length (auto-detected)


class OllamaClient:
    """
    Client for communicating with a remote Ollama server.
    
    Uses the OpenAI-compatible API provided by Ollama.
    Validates connectivity and model availability on initialization.
    """
    
    # Known-answer test for validation
    VALIDATION_PROMPT = "What is 2 + 2? Answer with just the number."
    VALIDATION_EXPECTED = "4"
    
    def __init__(self, config: OllamaConfig):
        """
        Initialize the Ollama client.
        
        Args:
            config: OllamaConfig with server URL, model name, etc.
            
        Raises:
            OllamaConnectionError: If server is unreachable
            OllamaModelNotFoundError: If model is not available
            OllamaValidationError: If known-answer test fails
        """
        self.config = config
        self.base_url = config.url.rstrip('/')
        self._concurrency_sem = threading.Semaphore(max(1, config.max_parallel_requests))
        
        # Validate everything on init
        self._validate_connection()
        self._validate_model()
        self._validate_handshake()
        
        logger.info(f"Ollama client initialized: {self.base_url} model={config.model} timeout={config.timeout}s")
    
    def _make_request(
        self, 
        endpoint: str, 
        method: str = "GET",
        data: Optional[Dict] = None,
        timeout: Optional[int] = None
    ) -> Dict:
        """
        Make an HTTP request to the Ollama server.
        
        Args:
            endpoint: API endpoint (e.g., "/api/tags")
            method: HTTP method
            data: Request body (will be JSON-encoded)
            timeout: Request timeout in seconds
            
        Returns:
            Parsed JSON response
            
        Raises:
            OllamaConnectionError: On connection failure
        """
        url = f"{self.base_url}{endpoint}"
        timeout = timeout or self.config.timeout
        
        headers = {"Content-Type": "application/json"}
        body = json.dumps(data).encode('utf-8') if data else None
        
        request = Request(url, data=body, headers=headers, method=method)
        
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else str(e)
            raise OllamaConnectionError(
                f"HTTP {e.code} from {url}: {error_body}"
            ) from e
        except URLError as e:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama server at {self.base_url}: {e.reason}"
            ) from e
        except json.JSONDecodeError as e:
            raise OllamaConnectionError(
                f"Invalid JSON response from {url}: {e}"
            ) from e
    
    def _make_streaming_request(
        self,
        endpoint: str,
        data: Dict,
        timeout: Optional[int] = None
    ) -> str:
        """
        Make a streaming request to the Ollama server and collect the full response.
        
        Ollama's generate/chat endpoints stream JSON objects line by line.
        
        Args:
            endpoint: API endpoint
            data: Request body
            timeout: Request timeout
            
        Returns:
            Complete response text
        """
        url = f"{self.base_url}{endpoint}"
        timeout = timeout or self.config.timeout
        
        headers = {"Content-Type": "application/json"}
        body = json.dumps(data).encode('utf-8')
        
        request = Request(url, data=body, headers=headers, method="POST")

        monitor_stop_event: Optional[threading.Event] = None
        monitor_thread: Optional[threading.Thread] = None
        if self.config.ps_monitor_interval and self.config.ps_monitor_interval > 0:
            monitor_stop_event = threading.Event()
            monitor_thread = threading.Thread(
                target=self._ps_monitor_loop,
                args=(monitor_stop_event, float(self.config.ps_monitor_interval)),
                name="ollama-ps-monitor",
                daemon=True,
            )
            monitor_thread.start()
        semaphore_timeout = min(timeout or self.config.timeout, self.config.timeout)
        acquired = self._concurrency_sem.acquire(timeout=semaphore_timeout)
        if not acquired:
            raise OllamaConnectionError(
                f"Timed out waiting for Ollama concurrency slot ({self.config.max_parallel_requests} max)."
            )
        try:
            with urlopen(request, timeout=timeout) as response:
                full_response = []
                for line in response:
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line.decode('utf-8'))
                        # Handle different response formats
                        if 'message' in chunk:
                            # Chat API format
                            content = chunk.get('message', {}).get('content', '')
                            if content:
                                full_response.append(content)
                        elif 'response' in chunk:
                            # Generate API format
                            full_response.append(chunk['response'])
                    except json.JSONDecodeError:
                        continue
                return ''.join(full_response)
        except HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else str(e)
            raise OllamaConnectionError(
                f"HTTP {e.code} from {url}: {error_body}"
            ) from e
        except URLError as e:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama server at {self.base_url}: {e.reason}"
            ) from e
        finally:
            if monitor_stop_event:
                monitor_stop_event.set()
            if monitor_thread:
                monitor_thread.join(timeout=1.0)
            self._concurrency_sem.release()

    def _apply_batching_options(self, options: Dict[str, Any], prompt_chars: int) -> Dict[str, Any]:
        """Inject num_batch when configured or when adaptive batching is enabled."""
        num_batch = self._resolve_num_batch(prompt_chars)
        if num_batch:
            options['num_batch'] = num_batch
            logger.debug(
                "Using num_batch=%s for prompt of approx %s chars",
                num_batch,
                prompt_chars,
            )
        return options

    def _resolve_num_batch(self, prompt_chars: int) -> Optional[int]:
        """Determine the num_batch value based on config and prompt size."""
        if self.config.num_batch is not None:
            try:
                return max(1, int(self.config.num_batch))
            except (TypeError, ValueError):
                return 1
        if not self.config.adaptive_batching:
            return None
        try:
            min_chars = max(1, int(self.config.adaptive_batch_min_chars))
            max_chars = max(min_chars, int(self.config.adaptive_batch_max_chars))
            min_batch = max(1, int(self.config.adaptive_batch_min))
            max_batch = max(min_batch, int(self.config.adaptive_batch_max))
        except (TypeError, ValueError):
            return None
        if prompt_chars <= min_chars:
            return min_batch
        if prompt_chars >= max_chars:
            return max_batch
        span = max_chars - min_chars
        ratio = (prompt_chars - min_chars) / span if span else 0.0
        scaled = min_batch + ratio * (max_batch - min_batch)
        return max(min_batch, min(max_batch, int(round(scaled))))

    @staticmethod
    def _estimate_message_chars(messages: List[Dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            content = msg.get('content', '')
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += len(str(part.get('text', '')))
                    elif isinstance(part, str):
                        total += len(part)
            else:
                total += len(str(content))
        return total

    def _ps_monitor_loop(self, stop_event: threading.Event, interval: float) -> None:
        """Periodically log /api/ps stats while a request is in flight."""
        # Delay first print slightly so short calls do not spam
        if stop_event.wait(interval):
            return
        while not stop_event.is_set():
            line = self._build_ps_line()
            if line:
                print(line, flush=True)
            if stop_event.wait(interval):
                break

    def _build_ps_line(self) -> Optional[str]:
        try:
            ps_data = self._make_request("/api/ps", timeout=5)
        except Exception as exc:
            logger.debug("Unable to fetch /api/ps: %s", exc)
            return None
        models = ps_data.get('models') or []
        if not models:
            return "[Ollama ps] no models loaded"
        total_vram = sum((m.get('size_vram') or m.get('size') or 0) for m in models)
        parts = []
        for model in models[:2]:
            name = model.get('name') or model.get('model') or 'unknown'
            size = model.get('size_vram') or model.get('size') or 0
            parts.append(f"{name}:{self._human_bytes(size)}")
        if len(models) > 2:
            parts.append(f"+{len(models)-2} more")
        return (
            f"[Ollama ps] loaded={len(models)} VRAM={self._human_bytes(total_vram)} :: "
            + ' | '.join(parts)
        )

    @staticmethod
    def _human_bytes(num_bytes: int) -> str:
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        value = float(num_bytes)
        for unit in units:
            if value < 1024.0 or unit == units[-1]:
                return f"{value:.1f}{unit}"
            value /= 1024.0
    
    def _validate_connection(self) -> None:
        """
        Validate that the Ollama server is reachable.
        
        Raises:
            OllamaConnectionError: If server is unreachable
        """
        logger.info(f"Validating connection to Ollama server at {self.base_url}...")
        
        try:
            # Use a short timeout for the health check
            response = self._make_request("/api/tags", timeout=10)
            logger.info("Ollama server is reachable")
        except OllamaConnectionError as e:
            raise OllamaConnectionError(
                f"\n{'='*60}\n"
                f"ERROR: Cannot connect to Ollama server\n"
                f"{'='*60}\n\n"
                f"URL: {self.base_url}\n"
                f"Error: {e}\n\n"
                f"REMEDIATION:\n"
                f"1. Ensure Ollama is running on the remote server\n"
                f"2. Check that the URL in config.yaml is correct\n"
                f"3. Verify network connectivity to the server\n"
                f"4. Check if a firewall is blocking port 11434\n"
                f"5. Ensure Ollama is configured to accept external connections\n\n"
                f"QUICK START (one-time command):\n"
                f"  OLLAMA_HOST=0.0.0.0 ollama serve\n\n"
                f"RUN AS SERVICE (persistent, survives reboots):\n\n"
                f"  Linux (systemd):\n"
                f"    sudo systemctl edit ollama.service\n"
                f"    # Add these lines:\n"
                f"    [Service]\n"
                f"    Environment=\"OLLAMA_HOST=0.0.0.0:11434\"\n"
                f"    \n"
                f"    sudo systemctl daemon-reload\n"
                f"    sudo systemctl restart ollama\n"
                f"    sudo systemctl status ollama\n\n"
                f"  Or create /etc/systemd/system/ollama.service.d/override.conf:\n"
                f"    [Service]\n"
                f"    Environment=\"OLLAMA_HOST=0.0.0.0:11434\"\n\n"
                f"  macOS (launchd):\n"
                f"    # Edit ~/.ollama/config or set in environment\n"
                f"    export OLLAMA_HOST=0.0.0.0:11434\n"
                f"    ollama serve &\n\n"
                f"VERIFY IT'S WORKING:\n"
                f"  curl http://<server-ip>:11434/api/tags\n"
                f"  # Should return JSON with available models\n"
                f"{'='*60}\n"
            ) from e
    
    def _validate_model(self) -> None:
        """
        Validate that the requested model is available on the server.
        
        Raises:
            OllamaModelNotFoundError: If model is not available
        """
        logger.info(f"Checking if model '{self.config.model}' is available...")
        
        response = self._make_request("/api/tags")
        available_models = [m['name'] for m in response.get('models', [])]
        
        # Check for exact match ONLY - size tags matter!
        model_name = self.config.model
        model_base = model_name.split(':')[0]
        
        # Exact match required
        found = model_name in available_models
        
        # If not found, check if a similar model exists (for helpful error message)
        similar_models = [m for m in available_models if m.startswith(f"{model_base}:")]
        
        if not found:
            model_list = '\n  - '.join(available_models) if available_models else "(no models installed)"
            
            # Build helpful error message
            error_msg = (
                f"\n{'='*60}\n"
                f"ERROR: Model not found on Ollama server\n"
                f"{'='*60}\n\n"
                f"Requested model: {model_name}\n"
                f"Server URL: {self.base_url}\n\n"
            )
            
            # If similar models exist, highlight them
            if similar_models:
                similar_list = '\n  - '.join(similar_models)
                error_msg += (
                    f"⚠️  SIMILAR MODELS FOUND (wrong size):\n  - {similar_list}\n\n"
                    f"NOTE: Model size tags MUST match exactly!\n"
                    f"'{model_name}' is different from '{similar_models[0]}'\n\n"
                )
            
            error_msg += (
                f"All available models:\n  - {model_list}\n\n"
                f"REMEDIATION:\n"
                f"1. Update config.yaml to use an available model:\n"
                f"   model: \"{similar_models[0] if similar_models else available_models[0] if available_models else model_name}\"\n\n"
                f"2. OR pull the exact model on the Ollama server:\n"
                f"   ollama pull {model_name}\n\n"
                f"For code review, recommended models:\n"
                f"  - qwen2.5-coder:32b (best for code)\n"
                f"  - qwen3-coder:30b (newer, slightly smaller)\n"
                f"  - codellama:34b\n"
                f"  - deepseek-coder:33b\n"
                f"{'='*60}\n"
            )
            
            raise OllamaModelNotFoundError(error_msg)
        
        logger.info(f"Model '{model_name}' is available (exact match verified)")
        
        # Try to get model's context length
        self._fetch_model_context_length()
    
    def _fetch_model_context_length(self) -> None:
        """Fetch model details to get context length (num_ctx)."""
        try:
            data = {"name": self.config.model}
            response = self._make_request("/api/show", method="POST", data=data, timeout=30)
            
            # Extract num_ctx from model_info.parameters or modelfile
            model_info = response.get('model_info', {})
            parameters = response.get('parameters', '')
            
            # First try model_info (structured data)
            for key, value in model_info.items():
                if 'context' in key.lower() or key == 'num_ctx':
                    try:
                        self.config.num_ctx = int(value)
                        logger.info(f"Model context length from model_info: {self.config.num_ctx} tokens")
                        return
                    except (ValueError, TypeError):
                        pass
            
            # Try parsing parameters string (e.g., "num_ctx 4096")
            if parameters:
                import re
                match = re.search(r'num_ctx\s+(\d+)', parameters)
                if match:
                    self.config.num_ctx = int(match.group(1))
                    logger.info(f"Model context length from parameters: {self.config.num_ctx} tokens")
                    return
            
            logger.debug("Could not determine model context length, will use defaults")
            
        except Exception as e:
            logger.debug(f"Failed to fetch model info: {e}")
    
    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count using a simple heuristic (4 chars per token)."""
        return len(text) // 4
    
    def _calculate_max_tokens(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        prompt: Optional[str] = None,
        requested_max_tokens: Optional[int] = None
    ) -> int:
        """
        Calculate appropriate max_tokens based on input size and model limits.
        
        Returns:
            Appropriate max_tokens value that won't exceed model limits
        """
        max_tokens = requested_max_tokens or self.config.max_tokens
        
        if not self.config.num_ctx:
            return max_tokens
        
        # Estimate input tokens
        if messages:
            input_text = ''.join(msg.get('content', '') for msg in messages)
        elif prompt:
            input_text = prompt
        else:
            return max_tokens
        
        estimated_input_tokens = self._estimate_tokens(input_text)
        
        # Calculate available tokens (leave 5% margin)
        safety_margin = int(self.config.num_ctx * 0.05)
        available_tokens = self.config.num_ctx - estimated_input_tokens - safety_margin
        
        # Ensure at least some tokens for output
        available_tokens = max(256, available_tokens)
        
        final_max_tokens = min(max_tokens, available_tokens)
        
        if final_max_tokens < max_tokens:
            logger.info(
                f"Adjusted max_tokens from {max_tokens} to {final_max_tokens} "
                f"(input ~{estimated_input_tokens} tokens, model limit {self.config.num_ctx})"
            )
        
        return final_max_tokens
    
    def _validate_handshake(self) -> None:
        """
        Perform a known-answer handshake to verify the model is working.
        
        Raises:
            OllamaValidationError: If handshake fails
        """
        logger.info("Performing known-answer handshake test...")
        
        try:
            response = self.generate(self.VALIDATION_PROMPT, max_tokens=10)
            
            # Check if the response contains "4" anywhere
            if self.VALIDATION_EXPECTED not in response:
                raise OllamaValidationError(
                    f"\n{'='*60}\n"
                    f"ERROR: Model handshake validation failed\n"
                    f"{'='*60}\n\n"
                    f"Test prompt: {self.VALIDATION_PROMPT}\n"
                    f"Expected response containing: {self.VALIDATION_EXPECTED}\n"
                    f"Actual response: {response}\n\n"
                    f"The model is accessible but may not be functioning correctly.\n"
                    f"This could indicate a model loading issue on the server.\n\n"
                    f"REMEDIATION:\n"
                    f"1. Check Ollama server logs for errors\n"
                    f"2. Try restarting the Ollama service\n"
                    f"3. Ensure sufficient GPU/RAM on the server\n"
                    f"{'='*60}\n"
                )
            
            logger.info("Handshake test passed")
            
        except OllamaConnectionError as e:
            raise OllamaValidationError(
                f"Handshake failed due to connection error: {e}"
            ) from e
    
    def generate(
        self, 
        prompt: str, 
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """
        Generate a response from the model.
        
        Args:
            prompt: Input prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            
        Returns:
            Generated text
        """
        effective_max_tokens = self._calculate_max_tokens(
            prompt=prompt,
            requested_max_tokens=max_tokens
        )
        option_payload = dict(self.config.extra_options)
        option_payload["num_predict"] = effective_max_tokens
        option_payload["temperature"] = (
            temperature if temperature is not None else self.config.temperature
        )
        option_payload = self._apply_batching_options(option_payload, len(prompt or ""))
        data = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": True,
            "options": option_payload,
        }
        
        return self._make_streaming_request("/api/generate", data)
    
    def chat(
        self, 
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """
        Send a chat completion request.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            
        Returns:
            Assistant's response text
        """
        effective_max_tokens = self._calculate_max_tokens(
            messages=messages,
            requested_max_tokens=max_tokens
        )
        option_payload = dict(self.config.extra_options)
        option_payload["num_predict"] = effective_max_tokens
        option_payload["temperature"] = (
            temperature if temperature is not None else self.config.temperature
        )
        option_payload = self._apply_batching_options(
            option_payload,
            self._estimate_message_chars(messages),
        )
        data = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "options": option_payload,
        }
        
        start_time = time.time()
        logger.debug(f"Sending chat request with {len(messages)} messages...")
        
        # Estimate context size for timeout warning
        total_chars = sum(len(m.get('content', '')) for m in messages)
        if total_chars > 30000:
            logger.warning(f"Large context ({total_chars} chars) - may take several minutes...")
        
        response = self._make_streaming_request("/api/chat", data)
        
        elapsed = time.time() - start_time
        logger.info(f"Chat response received in {elapsed:.1f}s ({len(response)} chars)")
        
        return response
    
    def list_models(self) -> List[str]:
        """
        List available models on the server.
        
        Returns:
            List of model names
        """
        response = self._make_request("/api/tags")
        return [m['name'] for m in response.get('models', [])]
    
    def get_server_metrics(self) -> Dict[str, Any]:
        """
        Fetch server metrics from Ollama's /api/ps endpoint.
        
        Returns dict with:
            - vram_used: int (bytes of VRAM used by loaded models)
            - vram_total: int (estimated total VRAM, 0 if unknown)
            - models_loaded: int (number of models in memory)
            - context_length: int (context length of current model)
            - available_capacity: int (estimated additional parallel requests)
        """
        metrics = {
            'vram_used': 0,
            'vram_total': 0,
            'models_loaded': 0,
            'context_length': self.config.num_ctx or 4096,
            'available_capacity': 1,
        }
        
        try:
            response = self._make_request("/api/ps", timeout=10)
            models = response.get('models', [])
            
            metrics['models_loaded'] = len(models)
            
            for model in models:
                # size_vram is the GPU memory used by this model
                vram = model.get('size_vram', 0)
                if vram:
                    metrics['vram_used'] += vram
                
                # Get context length if available
                ctx = model.get('context_length', 0)
                if ctx:
                    metrics['context_length'] = ctx
            
            # Estimate capacity based on VRAM usage
            # This is rough - Ollama doesn't expose total VRAM directly
            # But we can infer from the model size and typical GPU sizes
            if metrics['vram_used'] > 0:
                # Assume common GPU sizes: 8GB, 12GB, 16GB, 24GB, 48GB, 80GB
                # Estimate based on what fraction of VRAM is used
                vram_gb = metrics['vram_used'] / (1024**3)
                
                # Conservative estimate: if model uses X GB, assume GPU has ~2X GB
                # This gives us room for 1-2 parallel requests
                estimated_total = metrics['vram_used'] * 2
                vram_free_ratio = 1.0 - (metrics['vram_used'] / estimated_total)
                
                # Each additional request needs roughly 10-20% more VRAM for KV cache
                # depending on context size
                estimated_slots = max(1, int(vram_free_ratio / 0.15))
                metrics['available_capacity'] = min(estimated_slots, 4)  # Cap at 4 for Ollama
            else:
                # No VRAM info, assume single GPU with some capacity
                metrics['available_capacity'] = 2
            
            logger.debug(
                f"Ollama metrics: VRAM used={metrics['vram_used']/(1024**3):.1f}GB, "
                f"models={metrics['models_loaded']}, capacity={metrics['available_capacity']}"
            )
            
        except Exception as e:
            logger.debug(f"Failed to fetch Ollama metrics: {e}")
            metrics['available_capacity'] = 2
        
        return metrics
    
    def get_recommended_parallelism(self, max_parallel: int = 4) -> int:
        """
        Get recommended number of parallel requests based on server metrics.
        
        Args:
            max_parallel: Maximum parallelism cap (default 4 for Ollama)
            
        Returns:
            Recommended number of parallel requests (1 to max_parallel)
        """
        metrics = self.get_server_metrics()
        
        # Ollama is generally less efficient with parallel requests than vLLM
        # So we're more conservative here
        recommended = min(metrics['available_capacity'], max_parallel)
        
        # If context length is very large, reduce parallelism
        if metrics['context_length'] > 32000:
            recommended = min(recommended, 2)
        elif metrics['context_length'] > 16000:
            recommended = min(recommended, 3)
        
        recommended = max(1, recommended)
        
        logger.info(f"Recommended parallelism: {recommended} (context: {metrics['context_length']})")
        return recommended


def create_client_from_config(config_dict: Dict[str, Any]) -> OllamaClient:
    """
    Create an OllamaClient from a configuration dictionary.
    
    Args:
        config_dict: Dictionary with 'ollama' section containing url, model, etc.
        
    Returns:
        Configured OllamaClient
    """
    ollama_config = config_dict.get('ollama', {})

    # Allow environment variables to override config values for deployments
    env_url = os.environ.get('ANGRY_AI_OLLAMA_URL') or os.environ.get('OLLAMA_URL')
    env_model = os.environ.get('ANGRY_AI_OLLAMA_MODEL') or os.environ.get('OLLAMA_MODEL')
    env_ps_interval = os.environ.get('ANGRY_AI_OLLAMA_PS_INTERVAL')
    env_parallel = os.environ.get('ANGRY_AI_OLLAMA_MAX_PARALLEL')

    batching_cfg = ollama_config.get('batching', {})
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

    env_num_batch = os.environ.get('ANGRY_AI_OLLAMA_NUM_BATCH')
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

    extra_options = ollama_config.get('options', {})
    if not isinstance(extra_options, dict):
        extra_options = {}

    if env_ps_interval is not None:
        try:
            ps_interval = float(env_ps_interval)
        except ValueError:
            ps_interval = ollama_config.get('ps_monitor_interval', 0)
    else:
        ps_interval = ollama_config.get('ps_monitor_interval', 0)

    if env_parallel is not None:
        try:
            max_parallel = int(env_parallel)
        except ValueError:
            max_parallel = _coerce_int(batching_cfg.get('max_parallel_requests', 1), 1)
    else:
        max_parallel = _coerce_int(batching_cfg.get('max_parallel_requests', 1), 1)

    config = OllamaConfig(
        url=env_url or ollama_config.get('url', 'http://localhost:11434'),
        model=env_model or ollama_config.get('model', 'qwen2.5-coder:32b'),
        timeout=ollama_config.get('timeout', 300),
        max_tokens=ollama_config.get('max_tokens', 4096),
        temperature=ollama_config.get('temperature', 0.1),
        num_batch=num_batch,
        adaptive_batching=_coerce_bool(batching_cfg.get('adaptive'), False) if num_batch is None else False,
        adaptive_batch_min_chars=_coerce_int(batching_cfg.get('min_chars', 8000), 8000),
        adaptive_batch_max_chars=_coerce_int(batching_cfg.get('max_chars', 60000), 60000),
        adaptive_batch_min=_coerce_int(batching_cfg.get('min_num_batch', 2), 2),
        adaptive_batch_max=_coerce_int(batching_cfg.get('max_num_batch', 8), 8),
        extra_options=extra_options,
        ps_monitor_interval=max(0.0, float(ps_interval or 0)),
        max_parallel_requests=max(1, int(max_parallel or 1)),
    )
    
    return OllamaClient(config)


if __name__ == "__main__":
    # Self-test: validate connection to a local Ollama server
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    if os.environ.get('OLLAMA_SELFTEST', '').lower() not in {'1', 'true', 'yes'}:
        print("Ollama self-test skipped (set OLLAMA_SELFTEST=1 to run).")
        sys.exit(0)
    
    print("Testing Ollama client...")
    default_url = os.environ.get('ANGRY_AI_OLLAMA_URL') or os.environ.get('OLLAMA_URL') or "http://localhost:11434"
    default_model = os.environ.get('ANGRY_AI_OLLAMA_MODEL') or os.environ.get('OLLAMA_MODEL') or "qwen2.5-coder:32b"
    print(f"Default URL: {default_url}")
    print()
    
    try:
        config = OllamaConfig(
            url=default_url,
            model=default_model,
        )
        client = OllamaClient(config)
        
        print("\n✓ All validation checks passed!")
        print(f"\nAvailable models: {', '.join(client.list_models())}")
        
    except OllamaError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

