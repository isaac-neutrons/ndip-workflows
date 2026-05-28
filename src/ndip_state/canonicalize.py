"""Rewrite realpath'd paths back to the operator-supplied prefix.

The pipeline tools (``simple-reduction``, ``plan-data``, ``analyze-sample``,
``data-assembler ingest``, ``nr-isaac-format convert-ingest``) resolve their
output directory through ``os.path.realpath`` and embed the resolved path in the
artifacts/params they report. On SNS hosts ``/SNS/<INSTR>`` is a symlink to
``/gpfs/neutronsfs/instruments/<INSTR>``, so a tool may report
``/gpfs/...`` paths even though the operator supplied ``/SNS/...``.

This module walks an arbitrary JSON-like object and substitutes the resolved
prefix back to the canonical (operator-supplied) one. It is the single home for
logic that used to be duplicated as an inlined ``canonicalize_paths`` configfile
in ``tools/simple_analyzer.xml`` and ``tools/data_assembler.xml``.

Stdlib-only and dependency-free so it can be exercised in-process by tests and
called from the merge-in adapter layer.
"""

import json
import os


def _prefix_map(canonical):
    """Return ``(needle, replacement)`` for rewriting *resolved* → *canonical*.

    ``needle`` is the resolved-path prefix to search for and ``replacement`` is
    the canonical prefix to substitute in (both ending in ``os.sep``). Returns
    ``None`` when there is nothing to rewrite (no symlink, or no shared suffix).
    """
    if not canonical:
        return None

    resolved = os.path.realpath(canonical)
    if resolved == canonical:
        return None

    c_parts = canonical.split(os.sep)
    r_parts = resolved.split(os.sep)
    suffix = 0
    for i in range(1, min(len(c_parts), len(r_parts)) + 1):
        if c_parts[-i] == r_parts[-i]:
            suffix = i
        else:
            break
    if suffix == 0:
        return None

    c_prefix = os.sep.join(c_parts[:-suffix]) or os.sep
    r_prefix = os.sep.join(r_parts[:-suffix]) or os.sep
    if c_prefix == r_prefix:
        return None

    needle = r_prefix.rstrip(os.sep) + os.sep
    replacement = c_prefix.rstrip(os.sep) + os.sep
    return needle, replacement


def canonicalize_paths(obj, canonical):
    """Return *obj* with every string under the resolved prefix rewritten.

    *canonical* is the operator-supplied directory (e.g. ``$OUTPUT_DIR``). Any
    string anywhere in *obj* that begins with the realpath-resolved form of that
    directory's prefix is rewritten back to the canonical prefix. Strings that
    don't share the resolved prefix are left untouched. When there is nothing to
    rewrite, *obj* is returned unchanged (a deep copy is not guaranteed).
    """
    mapping = _prefix_map(canonical)
    if mapping is None:
        return obj
    needle, replacement = mapping

    def rewrite(node):
        if isinstance(node, dict):
            return {k: rewrite(v) for k, v in node.items()}
        if isinstance(node, list):
            return [rewrite(v) for v in node]
        if isinstance(node, str) and node.startswith(needle):
            return replacement + node[len(needle):]
        return node

    return rewrite(obj)


def canonicalize_file(path, canonical):
    """Rewrite a JSON file in place. No-op if *path* is missing or *canonical* empty."""
    if not canonical or not os.path.isfile(path):
        return
    with open(path) as f:
        data = json.load(f)
    data = canonicalize_paths(data, canonical)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
