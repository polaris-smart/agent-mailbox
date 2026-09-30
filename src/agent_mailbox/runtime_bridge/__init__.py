"""Bundled, locally installed ACP runtime assets; no service starts on import."""

from pathlib import Path


def bridge_path() -> Path:
    """Return the Node NDJSON bridge shipped with the Python package."""
    return Path(__file__).with_name("bridge.mjs")
