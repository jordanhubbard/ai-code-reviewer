#!/usr/bin/env python3
"""
vLLM Client for AI Code Reviewer

Handles communication with a remote vLLM server using the OpenAI-compatible API.
Provides early validation of server connectivity and model availability.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class VLLMError(Exception):
    """Base exception for vLLM-related errors."""
    pass


class VLLMConnectionError(VLLMError):
    """Raised when connection to vLLM server fails."""
    pass


class VLLMModelNotFoundError(VLLMError):
    """Raised when the requested model is not available on the server."""
    pass


@dataclass
class VLLMConfig:
    """Configuration for vLLM client."""
    url: str
    model: str
    timeout: int = 300
    max_tokens: int = 4096
    temperature: float = 0.1
    extra_options: Dict[str, Any] = field(default_factory=dict)
    max_model_len: Optional[int] = None  # Model's context length (auto-detected)


class VLLMClient:
    """
    Client for communicating with a remote vLLM server.
    
    Uses the OpenAI-compatible API provided by vLLM.
    Validates connectivity and model availability on initialization.
    """
    
    def __init__(self, config: VLLMConfig, skip_validation: bool = False):
        """
        Initialize the vLLM client.
        
        Args:
            config: VLLMConfig with server URL, model name, etc.
            skip_validation: Skip model validation (useful for probing)
            
        Raises:
            VLLMConnectionError: If server is unreachable
            VLLMModelNotFoundError: If model is not available
        """
        self.config = config
        self.base_url = config.url.rstrip('/')
        
        if not skip_validation:
            self._validate_connection()
            self._validate_model()
        
        logger.info(f"vLLM client initialized: {self.base_url} model={config.model}")
    
    def _make_request(
        self, 
        endpoint: str, 
        method: str = "GET",
        data: Optional[Dict] = None,
        timeout: Optional[int] = None
    ) -> Dict:
        """Make an HTTP request to the vLLM server."""
        url = f"{self.base_url}{endpoint}"
        timeout = timeout or self.config.timeout
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps(data).encode('utf-8') if data else None
        
        request = Request(url, data=body, headers=headers, method=method)
        
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else str(e)
            if e.code == 404 and 'does not exist' in error_body.lower():
                raise VLLMModelNotFoundError(
                    f"Model not found on {url}: {error_body}"
                ) from e
            raise VLLMConnectionError(
                f"HTTP {e.code} from {url}: {error_body}"
            ) from e
        except URLError as e:
            raise VLLMConnectionError(
                f"Cannot connect to vLLM server at {self.base_url}: {e.reason}"
            ) from e
        except json.JSONDecodeError as e:
            raise VLLMConnectionError(
                f"Invalid JSON response from {url}: {e}"
            ) from e
    
    def _make_streaming_request(
        self,
        endpoint: str,
        data: Dict,
        timeout: Optional[int] = None
    ) -> str:
        """Make a streaming request to the vLLM server and collect the full response."""
        url = f"{self.base_url}{endpoint}"
        timeout = timeout or self.config.timeout
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        body = json.dumps(data).encode('utf-8')
        
        request = Request(url, data=body, headers=headers, method="POST")
        
        try:
            with urlopen(request, timeout=timeout) as response:
                full_response = []
                for line in response:
                    line = line.decode('utf-8').strip()
                    if not line:
                        continue
                    if line.startswith('data: '):
                        line = line[6:]
                    if line == '[DONE]':
                        break
                    try:
                        chunk = json.loads(line)
                        # OpenAI streaming format
                        choices = chunk.get('choices', [])
                        if choices:
                            delta = choices[0].get('delta', {})
                            content = delta.get('content', '')
                            if content:
                                full_response.append(content)
                    except json.JSONDecodeError:
                        continue
                return ''.join(full_response)
        except HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else str(e)
            if e.code == 404 and 'does not exist' in error_body.lower():
                raise VLLMModelNotFoundError(
                    f"Model not found on {url}: {error_body}"
                ) from e
            raise VLLMConnectionError(
                f"HTTP {e.code} from {url}: {error_body}"
            ) from e
        except URLError as e:
            raise VLLMConnectionError(
                f"Cannot connect to vLLM server at {self.base_url}: {e.reason}"
            ) from e
    
    def _make_non_streaming_request(
        self,
        endpoint: str,
        data: Dict,
        timeout: Optional[int] = None
    ) -> str:
        """Make a non-streaming request to the vLLM server."""
        data['stream'] = False
        response = self._make_request(endpoint, method="POST", data=data, timeout=timeout)
        
        choices = response.get('choices', [])
        if choices:
            message = choices[0].get('message', {})
            return message.get('content', '')
        return ''
    
    def _validate_connection(self) -> None:
        """Validate that the vLLM server is reachable."""
        logger.info(f"Validating connection to vLLM server at {self.base_url}...")
        
        try:
            # vLLM uses OpenAI-compatible /v1/models endpoint
            self._make_request("/v1/models", timeout=10)
            logger.info("vLLM server is reachable")
        except VLLMConnectionError as e:
            raise VLLMConnectionError(
                f"Cannot connect to vLLM server at {self.base_url}: {e}"
            ) from e
    
    def _validate_model(self) -> None:
        """Validate that the requested model is available on the server."""
        logger.info(f"Checking if model '{self.config.model}' is available on vLLM...")
        
        try:
            response = self._make_request("/v1/models")
        except VLLMConnectionError as e:
            raise VLLMModelNotFoundError(
                f"Cannot list models on vLLM server at {self.base_url}: {e}"
            ) from e
        
        model_data = response.get('data', [])
        available_models = [m.get('id', '') for m in model_data]
        model_name = self.config.model
        
        def _set_model_context_length(model_info: Dict) -> None:
            """Extract and store model's context length if available."""
            max_model_len = model_info.get('max_model_len')
            if max_model_len:
                self.config.max_model_len = int(max_model_len)
                logger.info(f"Model context length: {self.config.max_model_len} tokens")
        
        # Check for exact match first
        for model_info in model_data:
            if model_info.get('id') == model_name:
                logger.info(f"Model '{model_name}' found (exact match)")
                _set_model_context_length(model_info)
                return
        
        # Check for case-insensitive match
        model_lower = model_name.lower()
        for model_info in model_data:
            avail = model_info.get('id', '')
            if avail.lower() == model_lower:
                logger.info(f"Model '{model_name}' found as '{avail}' (case-insensitive match)")
                self.config.model = avail
                _set_model_context_length(model_info)
                return
        
        # Check for partial match (vLLM sometimes uses full paths)
        # Also normalize Ollama-style ":" to "-" for cross-format matching
        # e.g. config "qwen2.5-coder:32b" should match vLLM "Qwen/Qwen2.5-Coder-32B-Instruct"
        model_normalized = model_lower.replace(':', '-')
        for model_info in model_data:
            avail = model_info.get('id', '')
            avail_lower = avail.lower()
            avail_base = avail.split('/')[-1].lower()
            if (avail_base == model_lower or model_lower in avail_lower or
                    model_normalized in avail_lower):
                logger.info(f"Model '{model_name}' found as '{avail}' (partial match)")
                self.config.model = avail
                _set_model_context_length(model_info)
                return
        
        # Model not found - provide helpful error
        if available_models:
            model_list = '\n    - '.join(available_models)
            error_msg = (
                f"\n{'='*60}\n"
                f"Model '{model_name}' not found on vLLM server\n"
                f"{'='*60}\n"
                f"Server: {self.base_url}\n\n"
                f"Available models on this server:\n    - {model_list}\n\n"
                f"To fix, update config.yaml llm.models to use one of the above.\n"
                f"{'='*60}"
            )
        else:
            error_msg = (
                f"\n{'='*60}\n"
                f"No models loaded on vLLM server\n"
                f"{'='*60}\n"
                f"Server: {self.base_url}\n\n"
                f"The vLLM server is running but has no models loaded.\n"
                f"Check the vLLM server configuration and logs.\n"
                f"{'='*60}"
            )
        
        raise VLLMModelNotFoundError(error_msg)
    
    def _estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for text using a simple heuristic.
        
        This is a rough estimate (4 chars per token on average).
        For more accuracy, you'd use the actual tokenizer, but this
        avoids the dependency and is good enough for context management.
        """
        return len(text) // 4
    
    def _calculate_max_tokens(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        prompt: Optional[str] = None,
        requested_max_tokens: Optional[int] = None
    ) -> int:
        """
        Calculate appropriate max_tokens based on input size and model limits.
        
        Args:
            messages: Chat messages (for chat completions)
            prompt: Raw prompt (for completions)
            requested_max_tokens: User-requested max tokens
            
        Returns:
            Appropriate max_tokens value that won't exceed model limits
        """
        # Use configured max_tokens as default
        max_tokens = requested_max_tokens or self.config.max_tokens
        
        # If we don't know the model's context length, use the requested value
        if not self.config.max_model_len:
            return max_tokens
        
        # Estimate input tokens
        if messages:
            input_text = ''.join(
                msg.get('content', '') for msg in messages
            )
        elif prompt:
            input_text = prompt
        else:
            return max_tokens
        
        estimated_input_tokens = self._estimate_tokens(input_text)
        
        # Calculate available tokens (leave 5% margin for tokenizer variance)
        safety_margin = int(self.config.max_model_len * 0.05)
        available_tokens = self.config.max_model_len - estimated_input_tokens - safety_margin
        
        # Ensure at least some tokens for output
        min_output_tokens = 256
        available_tokens = max(min_output_tokens, available_tokens)
        
        # Use the smaller of requested and available
        final_max_tokens = min(max_tokens, available_tokens)
        
        if final_max_tokens < max_tokens:
            logger.info(
                f"Adjusted max_tokens from {max_tokens} to {final_max_tokens} "
                f"(input ~{estimated_input_tokens} tokens, model limit {self.config.max_model_len})"
            )
        
        return final_max_tokens
    
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
        
        data = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": effective_max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "stream": True,
            **self.config.extra_options,
        }
        
        start_time = time.time()
        logger.debug(f"Sending chat request with {len(messages)} messages...")
        
        try:
            response = self._make_streaming_request("/v1/chat/completions", data)
        except VLLMConnectionError:
            # Fall back to non-streaming if streaming fails
            logger.debug("Streaming failed, trying non-streaming request...")
            response = self._make_non_streaming_request("/v1/chat/completions", data)
        
        elapsed = time.time() - start_time
        logger.info(f"Chat response received in {elapsed:.1f}s ({len(response)} chars)")
        
        return response
    
    def generate(
        self, 
        prompt: str, 
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """
        Generate a response from the model using completions API.
        
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
        
        # vLLM supports /v1/completions for raw text generation
        data = {
            "model": self.config.model,
            "prompt": prompt,
            "max_tokens": effective_max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "stream": False,
            **self.config.extra_options,
        }
        
        response = self._make_request("/v1/completions", method="POST", data=data)
        
        choices = response.get('choices', [])
        if choices:
            return choices[0].get('text', '')
        return ''
    
    def list_models(self) -> List[str]:
        """List available models on the server."""
        response = self._make_request("/v1/models")
        return [m.get('id', '') for m in response.get('data', [])]

    def renegotiate_model(self) -> str:
        """
        Query the server for available models and switch to the first one.

        Called when a request fails with 404 model-not-found, indicating the
        server was restarted with a different model.

        Returns:
            The new model name

        Raises:
            VLLMModelNotFoundError: If no models are available on the server
        """
        try:
            response = self._make_request("/v1/models", timeout=10)
        except VLLMConnectionError as e:
            raise VLLMModelNotFoundError(
                f"Cannot list models during renegotiation: {e}"
            ) from e

        model_data = response.get('data', [])
        if not model_data:
            raise VLLMModelNotFoundError(
                f"No models available on server {self.base_url} during renegotiation"
            )

        new_model = model_data[0].get('id', '')
        old_model = self.config.model
        self.config.model = new_model

        # Update context length if available
        max_model_len = model_data[0].get('max_model_len')
        if max_model_len:
            self.config.max_model_len = int(max_model_len)

        logger.warning(
            f"Model renegotiated on {self.base_url}: '{old_model}' -> '{new_model}'"
        )
        return new_model
    
    def get_server_metrics(self) -> Dict[str, Any]:
        """
        Fetch server metrics from vLLM's /metrics endpoint.
        
        Returns dict with:
            - kv_cache_usage: float (0.0-1.0, percentage of KV cache used)
            - requests_running: int (number of requests currently running)
            - requests_waiting: int (number of requests waiting in queue)
            - gpu_cache_usage: float (0.0-1.0, GPU KV cache utilization)
            - available_capacity: int (estimated additional parallel requests)
        """
        metrics = {
            'kv_cache_usage': 0.0,
            'requests_running': 0,
            'requests_waiting': 0,
            'gpu_cache_usage': 0.0,
            'available_capacity': 1,
        }
        
        try:
            # vLLM exposes Prometheus metrics at /metrics
            url = f"{self.base_url}/metrics"
            request = Request(url, headers={"Accept": "text/plain"}, method="GET")
            
            with urlopen(request, timeout=10) as response:
                text = response.read().decode('utf-8')
                
                # Parse Prometheus format metrics
                for line in text.split('\n'):
                    if line.startswith('#'):
                        continue
                    
                    # vllm:gpu_cache_usage_perc or vllm:kv_cache_usage_perc
                    if 'kv_cache_usage_perc' in line or 'gpu_cache_usage_perc' in line:
                        try:
                            value = float(line.split()[-1])
                            metrics['kv_cache_usage'] = value
                            metrics['gpu_cache_usage'] = value
                        except (ValueError, IndexError):
                            pass
                    
                    # vllm:num_requests_running
                    elif 'num_requests_running' in line and not line.startswith('#'):
                        try:
                            value = int(float(line.split()[-1]))
                            metrics['requests_running'] = value
                        except (ValueError, IndexError):
                            pass
                    
                    # vllm:num_requests_waiting
                    elif 'num_requests_waiting' in line and not line.startswith('#'):
                        try:
                            value = int(float(line.split()[-1]))
                            metrics['requests_waiting'] = value
                        except (ValueError, IndexError):
                            pass
                
                # Calculate available capacity based on KV cache usage
                # Empirical observation: each request uses ~2-3% KV cache for typical
                # code review contexts. Be aggressive when KV cache is low.
                kv_used = metrics['kv_cache_usage']
                kv_free = 1.0 - kv_used
                
                if kv_used < 0.05:
                    # Nearly empty - allow many concurrent requests
                    # Start aggressive, the server will queue if needed
                    estimated_slots = 16
                elif kv_used < 0.30:
                    # Light load - use ~3% per request estimate
                    estimated_slots = max(8, int(kv_free / 0.03))
                elif kv_used < 0.60:
                    # Moderate load - use ~5% per request estimate
                    estimated_slots = max(4, int(kv_free / 0.05))
                else:
                    # Heavy load - use ~10% per request estimate
                    estimated_slots = max(2, int(kv_free / 0.10))
                
                # Subtract already waiting requests
                metrics['available_capacity'] = max(1, estimated_slots - metrics['requests_waiting'])
                
                logger.info(
                    f"vLLM metrics: KV={kv_used:.1%}, running={metrics['requests_running']}, "
                    f"waiting={metrics['requests_waiting']}, estimated_capacity={metrics['available_capacity']}"
                )
                
        except Exception as e:
            logger.debug(f"Failed to fetch vLLM metrics: {e}")
            # Return defaults - assume good capacity available
            # vLLM servers are typically powerful, be optimistic
            metrics['available_capacity'] = 8
        
        return metrics
    
    def get_recommended_parallelism(self, max_parallel: int = 16) -> int:
        """
        Get recommended number of parallel requests based on server metrics.
        
        Args:
            max_parallel: Maximum parallelism cap
            
        Returns:
            Recommended number of parallel requests (1 to max_parallel)
        """
        metrics = self.get_server_metrics()
        
        # Start with available capacity from metrics
        recommended = metrics['available_capacity']
        
        # If there are waiting requests, back off a bit
        if metrics['requests_waiting'] > 2:
            recommended = max(2, recommended - metrics['requests_waiting'])
        
        # Clamp to bounds
        recommended = max(1, min(recommended, max_parallel))
        
        logger.info(
            f"vLLM recommended parallelism: {recommended} "
            f"(KV={metrics['kv_cache_usage']:.1%}, running={metrics['requests_running']})"
        )
        return recommended
    
    @classmethod
    def probe_server(cls, url: str, timeout: int = 5) -> bool:
        """
        Probe if a URL is running a vLLM server (not Ollama).
        
        Both vLLM and Ollama support /v1/models (OpenAI-compatible),
        but only Ollama has /api/tags. So we check for Ollama first
        and return False if it looks like Ollama.
        
        Args:
            url: Base URL to probe
            timeout: Connection timeout
            
        Returns:
            True if vLLM server detected (not Ollama), False otherwise
        """
        url = url.rstrip('/')
        
        # First, check if this is an Ollama server (has /api/tags)
        try:
            ollama_request = Request(
                f"{url}/api/tags",
                headers={"Accept": "application/json"},
                method="GET"
            )
            with urlopen(ollama_request, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
                if 'models' in data:
                    # This is Ollama, not vLLM
                    return False
        except Exception:
            pass  # Not Ollama, continue to check for vLLM
        
        # Now check for vLLM's /v1/models endpoint
        try:
            request = Request(
                f"{url}/v1/models",
                headers={"Accept": "application/json"},
                method="GET"
            )
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
                # vLLM returns {"object": "list", "data": [...]}
                return data.get('object') == 'list' and 'data' in data
        except Exception:
            return False
