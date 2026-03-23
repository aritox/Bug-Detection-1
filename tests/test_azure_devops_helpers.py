import unittest
from unittest.mock import patch

from api import azure_devops


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, item_payloads):
        self.item_payloads = item_payloads
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None, auth=None):
        self.calls.append({"url": url, "headers": headers, "params": params, "auth": auth})
        if not url.endswith("/items"):
            return FakeResponse({"value": []})

        key = (params["path"], params["versionDescriptor.version"])
        payload = self.item_payloads.get(key)
        if payload is None:
            return FakeResponse({}, status_code=404)
        return FakeResponse(payload)


class TestAzureDevOpsHelpers(unittest.TestCase):
    def test_fetch_pull_request_details_uses_basic_auth(self):
        http = FakeHTTP({})

        payload = azure_devops.fetch_pull_request_details(
            http,
            "axefinanceQA",
            "QALab",
            "BugDetection",
            42,
            "token",
        )

        self.assertEqual(payload, {"value": []})
        self.assertTrue(http.calls)
        call = http.calls[0]
        self.assertTrue(call["url"].endswith("/pullrequests/42"))
        self.assertEqual(call["params"]["api-version"], azure_devops.API_VERSION)
        self.assertEqual(call["auth"].username, "")
        self.assertEqual(call["auth"].password, "token")

    def test_fetch_commit_diff_saves_debug_json(self):
        http = FakeHTTP({})

        with patch.object(azure_devops, "save_debug_json") as save_debug_json:
            payload = azure_devops.fetch_commit_diff(
                http,
                "axefinanceQA",
                "QALab",
                "BugDetection",
                "base-commit",
                "head-commit",
                "token",
            )

        self.assertEqual(payload, {"value": []})
        self.assertTrue(http.calls)
        self.assertTrue(http.calls[0]["url"].endswith("/diffs/commits"))
        self.assertEqual(http.calls[0]["params"]["baseVersion"], "base-commit")
        self.assertEqual(http.calls[0]["params"]["targetVersion"], "head-commit")
        save_debug_json.assert_called_once_with("pr_commit_diff.json", {"value": []})

    def test_extract_changed_paths_from_iteration_payload(self):
        paths = azure_devops.extract_changed_paths(
            {
                "changeEntries": [
                    {"changeType": "edit", "item": {"path": "/src/app.py"}},
                    {"changeType": "rename", "item": {"path": "/src/new_name.py"}},
                    {"changeType": "delete", "item": {"path": "/src/old.py"}},
                ]
            }
        )

        self.assertEqual(paths, ["src/app.py", "src/new_name.py", "src/old.py"])

    def test_build_unified_diff_text_handles_edit_add_and_delete(self):
        http = FakeHTTP(
            {
                ("/src/app.py", "base-commit"): {"content": "print('old')\n"},
                ("/src/app.py", "head-commit"): {"content": "print('new')\n"},
                ("/src/new.py", "head-commit"): {"content": "value = 1\n"},
                ("/src/dead.py", "base-commit"): {"content": "obsolete = True\n"},
            }
        )
        changes = [
            {"changeType": "edit", "item": {"path": "/src/app.py"}},
            {"changeType": "add", "item": {"path": "/src/new.py"}},
            {"changeType": "delete", "item": {"path": "/src/dead.py"}},
        ]

        with patch.object(azure_devops, "save_debug_text"):
            diff_text = azure_devops.build_unified_diff_text(
                http,
                "https://dev.azure.com/example/project/_apis/git/repositories/repo",
                "token",
                changes,
                "base-commit",
                "commit",
                "head-commit",
                "commit",
            )

        self.assertIn("diff --git a/src/app.py b/src/app.py", diff_text)
        self.assertIn("diff --git /dev/null b/src/new.py", diff_text)
        self.assertIn("diff --git a/src/dead.py /dev/null", diff_text)
        self.assertIn("+print('new')", diff_text)
        self.assertIn("-print('old')", diff_text)

    def test_format_compressed_diff_for_prompt_keeps_patch_headers(self):
        diff_text = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-old
+new
"""

        formatted = azure_devops.format_compressed_diff_for_prompt(diff_text)

        self.assertIn("File: src/a.py", formatted)
        self.assertIn("+new", formatted)


if __name__ == "__main__":
    unittest.main()
