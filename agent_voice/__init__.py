"""Voiccce package."""

from __future__ import annotations

# Literal fallback used when the package metadata is unavailable (e.g. running
# straight from a source checkout that was never installed). Keep in sync with
# the ``version`` in pyproject.toml; ``_resolve_version`` prefers the installed
# distribution's metadata so the two cannot silently drift once installed.
_FALLBACK_VERSION = "0.1.0"


def _resolve_version() -> str:
    """Return the installed distribution version, falling back to the literal.

    Single source of truth for ``voiccce --version``, ``status``, and ``update``.
    The metadata lookup is wrapped so a missing/legacy install never raises.
    """
    try:
        from importlib import metadata

        return metadata.version("voiccce")
    except Exception:  # pragma: no cover - metadata may be absent in source checkouts
        return _FALLBACK_VERSION


__version__ = _resolve_version()
