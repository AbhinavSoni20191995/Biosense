"""Where datasets live, and the boundary no HTTP handler may cross.

The rule this module exists to hold: **a private dataset is identified by where
it is, not by what it is called.** `serve.py` already refuses any file whose name
contains "truth", which works for a fixture the project named itself and is worth
nothing for a file a person uploaded. So privacy here is a path containment check
against a root, and `assert_disjoint` refuses a server configuration that would
put a runs directory inside the private root in the first place.
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import contracts as K

PRIVATE_ENV = 'BIOSENSE_PRIVATE_DATA'
CACHE_ENV = 'BIOSENSE_DATA_CACHE'

DEFAULT_PRIVATE = K.ROOT / 'private_data'
DEFAULT_CACHE = K.ROOT / 'data_cache'
FIXTURES = K.ROOT / 'examples' / 'datasets'


def private_root() -> Path:
    return Path(os.environ.get(PRIVATE_ENV) or DEFAULT_PRIVATE).resolve()


def cache_root() -> Path:
    return Path(os.environ.get(CACHE_ENV) or DEFAULT_CACHE).resolve()


def ensure_roots():
    for p in (private_root(), cache_root()):
        p.mkdir(parents=True, exist_ok=True)
    return private_root(), cache_root()


def _contains(root: Path, path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def is_private_path(path) -> bool:
    """True when *path* lies inside the private root.

    Resolved on both sides, so a symlink or a `..` cannot walk out of the root
    and present itself as public.
    """
    return _contains(private_root(), path)


def assert_servable(path):
    """Raise if *path* is private. The last check before bytes go to a socket."""
    if is_private_path(path):
        raise K.ContractError(
            f'{path} is inside the private data root and is never served over HTTP. '
            f'Private datasets stay on the machine that ingested them.')
    return path


def assert_disjoint(served_dir):
    """Refuse a server rooted inside the private root.

    A per-request check can only refuse what it is asked for. This refuses the
    configuration, which is the failure mode that would otherwise expose every
    private dataset at once.
    """
    served = Path(served_dir).resolve()
    priv = private_root()
    if _contains(priv, served) or _contains(served, priv):
        raise K.ContractError(
            f'refusing to serve {served}: it overlaps the private data root {priv}. '
            f'Move the runs directory, or point {PRIVATE_ENV} somewhere else.')
    return served
