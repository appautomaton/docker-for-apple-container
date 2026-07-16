from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from container_docker_shim.cli import print_help  # noqa: E402


class DocumentationConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (ROOT / "docs" / "index.html").read_text()
        self.readme = (ROOT / "README.md").read_text()

    def test_site_metadata_is_self_consistent(self) -> None:
        block = re.search(
            r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(block)
        schema = json.loads(block.group(1))  # type: ignore[union-attr]
        application = next(
            item
            for item in schema["@graph"]
            if item.get("@type") == "SoftwareApplication"
        )
        version = application["softwareVersion"]
        modified = application["dateModified"]
        self.assertIn(
            f"<span>v{version} · updated {modified}</span>",
            self.html,
        )
        sitemap = (ROOT / "docs" / "sitemap.xml").read_text()
        self.assertIn(f"<lastmod>{modified}</lastmod>", sitemap)

    def test_command_card_counts_match_their_details(self) -> None:
        for article in re.findall(
            r'<article class="card reveal">(.*?)</article>',
            self.html,
            re.DOTALL,
        ):
            summary = re.search(r"<summary>(\d+) more</summary>", article)
            if summary is None:
                continue
            details = article.split("<details", 1)[1]
            self.assertEqual(
                len(re.findall(r"<li\b", details)),
                int(summary.group(1)),
            )

    def test_image_list_is_classified_as_translated(self) -> None:
        articles = {}
        for article in re.findall(
            r'<article class="card reveal">(.*?)</article>',
            self.html,
            re.DOTALL,
        ):
            heading = re.search(r"<h3>(.*?)</h3>", article, re.DOTALL)
            if heading is not None:
                articles[heading.group(1)] = article
        self.assertIn("docker images / image ls", articles["Fully translated"])
        self.assertNotIn("docker images", articles["Thin passthrough"])

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
        self.assertIn("images, image inspect", help_text)
        self.assertNotIn("image <sub>", help_text)
        self.assertIn(
            "container inspect, container port, container prune",
            help_text,
        )


if __name__ == "__main__":
    unittest.main()
