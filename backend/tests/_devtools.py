"""
_devtools.py — Leak assertions for the redaction tests.
=====================================================
Shared by the test suite: collects every leaf value of a nested structure and
asserts none of them still contain a secret marker. Inspecting *leaf values*
(rather than ``str(result)`` or ``json.dumps(..., default=repr)``) is the only
reliable way to test redaction — a container's ``repr``/``default=`` hook can
reconstruct the original object and produce a false "leak".
"""
from __future__ import annotations

from typing import Any, Iterator

__all__ = ["leaves", "find_leaks"]


def leaves(value: Any, _depth: int = 0) -> Iterator[Any]:
    """Yield every leaf value inside a nested structure.

    Args:
        value: Any nested dict/list/tuple/set/scalar.
        _depth: Internal recursion guard.

    Yields:
        Scalar leaves (str, int, float, bool, None) reached by walking the
        structure. Containers are descended into, never yielded whole.
    """
    if _depth > 12:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from leaves(v, _depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from leaves(v, _depth + 1)
    else:
        yield value


def find_leaks(value: Any, needles: tuple[str, ...]) -> list[str]:
    """Return the distinct secret markers still present as leaf values.

    Args:
        value: Redacted structure to inspect.
        needles: Secret markers that must not survive redaction.

    Returns:
        List of markers that leaked (empty when redaction held).
    """
    found: list[str] = []
    for leaf in leaves(value):
        if isinstance(leaf, str):
            for needle in needles:
                if needle and needle in leaf and needle not in found:
                    found.append(needle)
    return found
