import unittest

from llm_client import LLMClient, ProviderConfig


class LLMClientTests(unittest.TestCase):
    def test_config_timeout_compatibility_property(self) -> None:
        client = LLMClient(
            providers=[ProviderConfig(url="http://127.0.0.1:1")],
            timeout=123,
        )

        self.assertIs(client.config, client)
        self.assertEqual(client.config.timeout, 123)
        client.shutdown()


if __name__ == "__main__":
    unittest.main()
