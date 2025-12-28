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
from dataclasses import dataclass
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
        data = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": True,
            "options": {
                "num_predict": max_tokens or self.config.max_tokens,
                "temperature": temperature if temperature is not None else self.config.temperature,
            }
        }
        
        return self._make_streaming_request("/api/generate", data)
    
    def chat(
        self, 
        messages: List[Dict[str, str]],
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
            "stream": True,
            "options": {
                "num_predict": max_tokens or self.config.max_tokens,
                "temperature": temperature if temperature is not None else self.config.temperature,
            }
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

    config = OllamaConfig(
        url=env_url or ollama_config.get('url', 'http://localhost:11434'),
        model=env_model or ollama_config.get('model', 'qwen2.5-coder:32b'),
        timeout=ollama_config.get('timeout', 300),
        max_tokens=ollama_config.get('max_tokens', 4096),
        temperature=ollama_config.get('temperature', 0.1),
    )
    
    return OllamaClient(config)


if __name__ == "__main__":
    # Self-test: validate connection to a local Ollama server
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    
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

