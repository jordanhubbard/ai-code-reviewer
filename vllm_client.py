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
        
        available_models = [m.get('id', '') for m in response.get('data', [])]
        model_name = self.config.model
        
        # Check for exact match first
        if model_name in available_models:
            logger.info(f"Model '{model_name}' found (exact match)")
            return
        
        # Check for case-insensitive match
        model_lower = model_name.lower()
        for avail in available_models:
            if avail.lower() == model_lower:
                logger.info(f"Model '{model_name}' found as '{avail}' (case-insensitive match)")
                # Update config to use the actual model name
                self.config.model = avail
                return
        
        # Check for partial match (vLLM sometimes uses full paths)
        for avail in available_models:
            avail_base = avail.split('/')[-1].lower()
            if avail_base == model_lower or model_lower in avail.lower():
                logger.info(f"Model '{model_name}' found as '{avail}' (partial match)")
                self.config.model = avail
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
        data = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens,
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
        # vLLM supports /v1/completions for raw text generation
        data = {
            "model": self.config.model,
            "prompt": prompt,
            "max_tokens": max_tokens or self.config.max_tokens,
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
    
    @classmethod
    def probe_server(cls, url: str, timeout: int = 5) -> bool:
        """
        Probe if a URL is running a vLLM server.
        
        Args:
            url: Base URL to probe
            timeout: Connection timeout
            
        Returns:
            True if vLLM server detected, False otherwise
        """
        url = url.rstrip('/')
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
