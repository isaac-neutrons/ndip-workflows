#!/usr/bin/env python3
"""Generate the Galaxy tool XMLs from their ``*.xml.in`` templates.

Each tool wrapper needs the orchestration shim (project-out / merge-in /
canonicalize) available at runtime inside its foreign container. Galaxy injects
it via a ``<configfile>``, so the shim source is inlined into every XML. To keep
a single source of truth, the committed ``tools/<name>.xml`` files are generated
from ``tools/<name>.xml.in`` by replacing the ``@NDIP_SHIM@`` marker with the
contents of ``tools/ndip_shim.py``.

Usage::

    python tools/build_tool_xmls.py            # regenerate all tools/*.xml

``tests/test_ndip_shim.py`` fails if a committed XML is out of date with the
template + shim, so regenerate after editing either.
"""

import pathlib

HERE = pathlib.Path(__file__).resolve().parent
MARKER = "@NDIP_SHIM@"


def shim_text():
    return (HERE / "ndip_shim.py").read_text().rstrip("\n")


def render(template_text, shim):
    if MARKER not in template_text:
        raise ValueError("template is missing the %s marker" % MARKER)
    return template_text.replace(MARKER, shim)


def templates():
    return sorted(HERE.glob("*.xml.in"))


def output_for(template_path):
    # reduction.xml.in -> reduction.xml
    return template_path.with_suffix("")


def build():
    shim = shim_text()
    written = []
    for tin in templates():
        out = output_for(tin)
        out.write_text(render(tin.read_text(), shim))
        written.append(out)
    return written


if __name__ == "__main__":
    for path in build():
        print("wrote %s" % path.name)
