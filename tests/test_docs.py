from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from container_docker_shim.cli import SUPPORTED_CONTAINER_VERSION, print_help  # noqa: E402


class DocumentationConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = (ROOT / "README.md").read_text()

    def test_runtime_compatibility_baseline_is_consistent(self) -> None:
        documents = {"README.md": self.readme}
        for name, document in documents.items():
            with self.subTest(document=name):
                self.assertIn(SUPPORTED_CONTAINER_VERSION, document)
                self.assertIn("no backward compatibility", document.lower())
                self.assertNotRegex(document, r"1\.[12]\.\d+|1\.3\.1 (?:or newer|or later|\+)")

    def test_readme_passthrough_does_not_claim_untranslated_commands(self) -> None:
        passthrough = self.readme.split("### Thin passthrough", 1)[1].split(
            "### Compose", 1
        )[0]
        self.assertNotIn(
            "`pull`/`rm`/`tag`/`push`/`save`/`load`/`prune`/`ls`",
            passthrough,
        )

    def test_cli_help_uses_the_same_image_classification(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_help()
        help_text = output.getvalue()
        self.assertIn(f"Supported runtime: Apple container {SUPPORTED_CONTAINER_VERSION} only", help_text)
        self.assertIn("images, image inspect", help_text)
        self.assertNotIn("image <sub>", help_text)
        self.assertIn(
            "container inspect, container port, container prune",
            help_text,
        )


if __name__ == "__main__":
    unittest.main()
