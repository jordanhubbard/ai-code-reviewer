import unittest

from reviewer import SecretScanner


class SecretScannerTests(unittest.TestCase):
    def test_tool_metadata_files_are_excluded(self) -> None:
        diff = """diff --git a/.ai-code-reviewer/REWRITE-INDEX.md b/.ai-code-reviewer/REWRITE-INDEX.md
+++ b/.ai-code-reviewer/REWRITE-INDEX.md
+  <!-- unit: {"files": ["tools/tools/this/path/looks/high/entropy/Makefile"]} -->
diff --git a/.ai-code-reviewer/metrics/session.json b/.ai-code-reviewer/metrics/session.json
+++ b/.ai-code-reviewer/metrics/session.json
+{"token_like": "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890ABCDEFGHIJK"}
"""

        self.assertEqual(SecretScanner.scan_diff(diff), [])

    def test_source_files_are_still_scanned(self) -> None:
        diff = """diff --git a/app.py b/app.py
+++ b/app.py
+api_key = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
"""

        findings = SecretScanner.scan_diff(diff)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], "app.py")
        self.assertEqual(findings[0][1], "API Key")


if __name__ == "__main__":
    unittest.main()
