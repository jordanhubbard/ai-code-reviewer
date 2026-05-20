import unittest

from llm_client import LLMClient, LLMContextLengthError, ProviderConfig


class LLMClientTests(unittest.TestCase):
    def test_config_timeout_compatibility_property(self) -> None:
        client = LLMClient(
            providers=[ProviderConfig(url="http://127.0.0.1:1")],
            timeout=123,
        )

        self.assertIs(client.config, client)
        self.assertEqual(client.config.timeout, 123)
        client.shutdown()

    def test_chat_caps_max_tokens_to_context_budget(self) -> None:
        client = LLMClient(
            providers=[ProviderConfig(url="http://127.0.0.1:1")],
            max_tokens=800,
            context_window=1200,
            context_safety_tokens=100,
            min_response_tokens=32,
        )
        captured = {}

        def fake_chat(payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "ok"}}]}

        client._try_chat_with_failover = fake_chat
        messages = [{"role": "user", "content": "x" * 1800}]

        result = client.chat(messages)

        expected = client.request_max_tokens_for(messages)
        self.assertEqual(result, "ok")
        self.assertEqual(captured["max_tokens"], expected)
        self.assertLess(captured["max_tokens"], 800)
        client.shutdown()

    def test_chat_rejects_prompt_that_cannot_fit_minimum_response(self) -> None:
        client = LLMClient(
            providers=[ProviderConfig(url="http://127.0.0.1:1")],
            max_tokens=800,
            context_window=1000,
            context_safety_tokens=100,
            min_response_tokens=128,
        )
        messages = [{"role": "user", "content": "x" * 4000}]

        with self.assertRaises(LLMContextLengthError):
            client.chat(messages)

        client.shutdown()


if __name__ == "__main__":
    unittest.main()
