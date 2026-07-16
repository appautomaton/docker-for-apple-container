#!/usr/bin/env python3
"""Stamp the current version and date into the static site.

The landing page is served statically, so its JSON-LD and footer must carry the
real version for crawlers and LLMs. Rather than hand-edit those, CI runs this
stamper at deploy time with the version resolved from the latest git tag. It
rewrites these spots in place:

  docs/index.html
    - JSON-LD  "softwareVersion"
    - JSON-LD  "dateModified"
    - the footer line  v<version> · updated <date>
  docs/sitemap.xml
    - <lastmod>

datePublished (first publish) is intentionally left alone. The script is
idempotent and dependency-free. Run it locally to refresh the committed files:

    python3 scripts/stamp_site.py 0.1.3 2026-07-16
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
INDEX = DOCS / "index.html"
SITEMAP = DOCS / "sitemap.xml"


def stamp(html: str, version: str, date: str) -> tuple[str, int]:
    """Return (new_html, replacements_made)."""
    count = 0

    def sub(pattern: str, repl: str, text: str) -> str:
        nonlocal count
        text, n = re.subn(pattern, repl, text, count=1)
        count += n
        return text

    # JSON-LD software version and modified date (values only, keys untouched).
    html = sub(r'("softwareVersion":\s*")[^"]*(")', rf"\g<1>{version}\g<2>", html)
    html = sub(r'("dateModified":\s*")[^"]*(")', rf"\g<1>{date}\g<2>", html)
    # Footer line: <span>v<version> · updated <date></span> (the only bare
    # <span> that begins with a literal "v").
    html = sub(
        r"(<span>v)[^<]*(</span>)",
        rf"\g<1>{version} · updated {date}\g<2>",
        html,
    )
    return html, count


def main(argv: list[str]) -> int:
    if not (2 <= len(argv) <= 3):
        print("usage: stamp_site.py <version> [date YYYY-MM-DD]", file=sys.stderr)
        return 2
    version = argv[1].lstrip("v")
    date = argv[2] if len(argv) == 3 else ""
    if not date:
        print("error: a date (YYYY-MM-DD) is required", file=sys.stderr)
        return 2

    # index.html: three spots.
    original = INDEX.read_text(encoding="utf-8")
    updated, count = stamp(original, version, date)
    if count != 3:
        print(
            f"error: stamped {count} of 3 spots in {INDEX.name}; "
            "the markers may have changed",
            file=sys.stderr,
        )
        return 1
    if updated != original:
        INDEX.write_text(updated, encoding="utf-8")

    # sitemap.xml: the <lastmod> date.
    sm = SITEMAP.read_text(encoding="utf-8")
    sm_new, n = re.subn(r"(<lastmod>)[^<]*(</lastmod>)", rf"\g<1>{date}\g<2>", sm, count=1)
    if n != 1:
        print(f"error: no <lastmod> found in {SITEMAP.name}", file=sys.stderr)
        return 1
    if sm_new != sm:
        SITEMAP.write_text(sm_new, encoding="utf-8")

    print(f"stamped {INDEX.name} + {SITEMAP.name}: version={version} date={date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
