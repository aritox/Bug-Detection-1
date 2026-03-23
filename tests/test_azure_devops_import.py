import importlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import sysconfig
import unittest
from unittest.mock import patch


class TestAzureDevOpsImport(unittest.TestCase):
    def test_import_uses_local_diff_compressor_when_stdlib_compression_is_preloaded(self):
        stdlib_compression_init = Path(sysconfig.get_path("stdlib")) / "compression" / "__init__.py"
        spec = spec_from_file_location("compression", stdlib_compression_init)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)

        stdlib_compression = module_from_spec(spec)
        spec.loader.exec_module(stdlib_compression)

        with patch.dict(sys.modules, {"compression": stdlib_compression}):
            sys.modules.pop("api.azure_devops", None)
            module = importlib.import_module("api.azure_devops")

        self.assertTrue(callable(module.compress_pr_diff))
        self.assertIn(".png", module.SKIP_EXT)


if __name__ == "__main__":
    unittest.main()
