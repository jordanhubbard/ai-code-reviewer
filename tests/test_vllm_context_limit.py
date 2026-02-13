import unittest
from unittest.mock import patch

from llm_client import HostConfig, MultiHostClient, MultiHostConfig
from vllm_client import VLLMContextLimitError, _parse_vllm_context_limit_error_message


class VLLMContextLimitTests(unittest.TestCase):
    def test_parse_vllm_context_limit_error_message(self) -> None:
        msg = (
            "'max_tokens' or 'max_completion_tokens' is too large: 4096. "
            "This model's maximum context length is 32768 tokens and your request has 28833 "
            "input tokens (4096 > 32768 - 28833). None"
        )
        self.assertEqual(
            _parse_vllm_context_limit_error_message(msg),
            (32768, 28833, 4096),
        )

    def test_multihost_retries_with_smaller_max_tokens(self) -> None:
        class StubVLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.max_tokens_seen = []

            def chat(self, messages, max_tokens=None, temperature=None) -> str:
                self.calls += 1
                self.max_tokens_seen.append(max_tokens)
                if self.calls == 1:
                    raise VLLMContextLimitError(
                        max_model_len=32768,
                        input_tokens=28833,
                        requested_max_tokens=4096,
                        raw_message="context limit",
                    )
                return "ok"

        stub = StubVLLM()

        def _init_hosts(self: MultiHostClient) -> None:
            self._vllm_hosts = [
                HostConfig(
                    url="http://example.invalid:8000",
                    backend="vllm",
                    model="test-model",
                    client=stub,
                )
            ]
            self._ollama_hosts = []

        cfg = MultiHostConfig(
            hosts=["http://example.invalid:8000"],
            models=["test-model"],
            max_parallel_requests=1,
            health_check_enabled=False,
        )

        with patch.object(MultiHostClient, "_initialize_hosts", _init_hosts), \
            patch.object(MultiHostClient, "_start_health_check_thread", lambda self: None), \
            patch.object(MultiHostClient, "_query_recommended_parallelism", lambda self: 1):
            client = MultiHostClient(cfg)
            out = client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(out, "ok")
        self.assertEqual(stub.calls, 2)
        self.assertIsNone(stub.max_tokens_seen[0])
        self.assertIsInstance(stub.max_tokens_seen[1], int)
        self.assertLess(stub.max_tokens_seen[1], 4096)
        self.assertEqual(len(client._unhealthy_hosts), 0)


if __name__ == "__main__":
    unittest.main()
