# docker-for-apple-container

This repository owns the stateless Docker CLI translator. Armada is a separate
deployment project in the sibling `armada/` directory.

## Runtime contract

- Support exactly Apple `container` **1.3.1** on macOS 26. Do not add backward
  compatibility paths or claim support for unverified newer releases.
- When the supported runtime advances, update the code, fixtures, and README.
  Refresh the centrally published website in
  `appautomaton/appautomaton.github.io/sites/docker-for-apple-container/public/`
  after a validated runtime release. Keep the JSON-LD version/date, footer, and
  sitemap `lastmod` together; run the central Docker site consistency test.
  This repository no longer owns a Pages site or publisher workflow. Verify CLI
  options and JSON shapes against that exact runtime before changing translations.
- Use the supported runtime's schema directly. Reject unexpected required
  fields instead of guessing historical field names or probing alternate
  schemas. Docker-facing command aliases remain part of the shim's interface.
- Keep Apple runtime state authoritative; do not add shim-owned runtime state.

## Verification

Run `python3 -m unittest discover -s tests -v` for translator changes. Use
read-only live checks or explicitly scoped disposable containers for runtime
verification. Do not mutate unrelated workloads or use global prune.
