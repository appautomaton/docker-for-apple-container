"""Docker CLI compatibility shim over Apple container."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    # Resolved from installed package metadata, which hatch-vcs derives from the
    # git tag at build time. There is no version string hardcoded here.
    __version__ = _pkg_version("docker-for-apple-container")
except PackageNotFoundError:
    # Running from a source checkout (the dev symlink or a Homebrew source
    # install), where no dist metadata exists. __version__ is not surfaced to
    # users, so a sentinel is fine.
    __version__ = "0.0.0+source"
