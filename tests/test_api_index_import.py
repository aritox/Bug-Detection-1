import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch


class FakeGroq:
    def __init__(self, api_key):
        self.api_key = api_key


class TestAPIIndexImport(unittest.TestCase):
    def test_api_index_imports_with_package_relative_prompt_builder(self):
        fake_groq_module = types.SimpleNamespace(Groq=FakeGroq)

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}, clear=False):
            with patch.dict(sys.modules, {"groq": fake_groq_module}):
                sys.modules.pop("api.index", None)
                module = importlib.import_module("api.index")

        self.assertTrue(hasattr(module, "build_main_review_prompt"))
        self.assertEqual(module.RULES_PATH.name, "rules.txt")


if __name__ == "__main__":
    unittest.main()
