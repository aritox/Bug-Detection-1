import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch


class FakeGroq:
    def __init__(self, api_key):
        self.api_key = api_key


class TestAzureDevOpsWebhook(unittest.TestCase):
    def _import_module(self):
        fake_groq_module = types.SimpleNamespace(Groq=FakeGroq)
        with patch.dict(
            os.environ,
            {
                "GROQ_API_KEY": "test-key",
                "ADO_ORG": "axefinanceQA",
                "ADO_PROJECT": "QALab",
                "ADO_REPO": "BugDetection",
                "ADO_PAT": "test-token",
                "ENABLE_REFLECTION": "false",
            },
            clear=False,
        ):
            with patch.dict(sys.modules, {"groq": fake_groq_module}):
                sys.modules.pop("api.index", None)
                return importlib.import_module("api.index")

    def test_webhook_processes_supported_pull_request_event(self):
        module = self._import_module()
        payload = {
            "eventType": "git.pullrequest.created",
            "resource": {
                "pullRequestId": 42,
                "repository": {
                    "id": "repo-id",
                    "name": "BugDetection",
                },
                "sourceRefName": "refs/heads/feature",
                "targetRefName": "refs/heads/main",
            },
        }
        pr_details = {
            "pullRequestId": 42,
            "title": "Switch to Azure DevOps",
            "description": "Use Azure PR service hooks.",
            "lastMergeSourceCommit": {"commitId": "head-commit"},
            "lastMergeTargetCommit": {"commitId": "base-commit"},
            "sourceRefName": "refs/heads/feature",
            "targetRefName": "refs/heads/main",
        }
        pr_changes = {
            "changeEntries": [
                {"item": {"path": "/src/app.py"}, "changeType": "edit"},
            ]
        }

        with patch.object(module.azure_devops, "fetch_pull_request_details", return_value=pr_details), \
             patch.object(module.azure_devops, "fetch_pull_request_iteration_changes", return_value=pr_changes), \
             patch.object(module.azure_devops, "fetch_commit_diff", return_value={"changes": []}), \
             patch.object(module.azure_devops, "build_unified_diff_text", return_value="diff --git a/src/app.py b/src/app.py"), \
             patch.object(module.azure_devops, "format_compressed_diff_for_prompt", return_value="File: src/app.py"), \
             patch.object(module, "call_groq_review", return_value="Overall Risk Level: Low"), \
             patch.object(module, "save_json_debug_file") as save_json_debug_file, \
             patch.object(module, "save_debug_artifacts"):
            response = module.app.test_client().post("/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "processed")
        self.assertEqual(response.get_json()["pullRequestId"], 42)
        self.assertTrue(response.get_json()["reviewGenerated"])
        self.assertEqual(response.get_json()["preview_url"], "/review-result")
        saved_files = [call_args.args[0] for call_args in save_json_debug_file.call_args_list]
        self.assertIn("azure_headers.json", saved_files)
        self.assertIn("azure_payload.json", saved_files)
        self.assertIn("pr_details.json", saved_files)

        review_page = module.app.test_client().get("/review-result")
        self.assertEqual(review_page.status_code, 200)
        self.assertIn("BugDetection", review_page.get_data(as_text=True))
        self.assertIn("refs/heads/feature", review_page.get_data(as_text=True))

    def test_webhook_ignores_unsupported_events(self):
        module = self._import_module()
        payload = {
            "eventType": "git.pullrequest.abandoned",
            "resource": {"pullRequestId": 42},
        }

        response = module.app.test_client().post("/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ignored")

    def test_webhook_allows_temporary_merged_event(self):
        module = self._import_module()
        payload = {
            "eventType": "git.pullrequest.merged",
            "resource": {
                "pullRequestId": 59,
                "repository": {"id": "repo-id", "name": "BugDetection"},
                "sourceRefName": "refs/heads/release",
                "targetRefName": "refs/heads/main",
            },
        }
        pr_details = {
            "pullRequestId": 59,
            "title": "Merged PR",
            "description": "Temporary merged-event test.",
            "lastMergeSourceCommit": {"commitId": "head-commit"},
            "lastMergeTargetCommit": {"commitId": "base-commit"},
            "sourceRefName": "refs/heads/release",
            "targetRefName": "refs/heads/main",
        }

        with patch.object(module.azure_devops, "fetch_pull_request_details", return_value=pr_details), \
             patch.object(module.azure_devops, "fetch_pull_request_iteration_changes", return_value={"changeEntries": []}), \
             patch.object(module.azure_devops, "fetch_commit_diff", return_value={"changes": []}), \
             patch.object(module.azure_devops, "build_unified_diff_text", return_value=""), \
             patch.object(module.azure_devops, "format_compressed_diff_for_prompt", return_value=""), \
             patch.object(module, "save_json_debug_file"), \
             patch.object(module, "save_debug_artifacts"):
            response = module.app.test_client().post("/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "processed")
        self.assertEqual(response.get_json()["eventType"], "git.pullrequest.merged")

    def test_webhook_accepts_supported_event_with_missing_resource(self):
        module = self._import_module()
        payload = {
            "eventType": "git.pullrequest.updated",
        }

        response = module.app.test_client().post("/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "accepted")
        self.assertEqual(response.get_json()["reason"], "missing resource")


if __name__ == "__main__":
    unittest.main()
