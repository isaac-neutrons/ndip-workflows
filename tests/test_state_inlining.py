"""Verify the inlined `state_module` configfile in each Galaxy tool XML
stays in sync with the canonical ``src/ndip_state/state.py``.

The tool XMLs embed a verbatim copy of ``state.py`` inside a
``<configfile name="state_module">#raw ... #end raw</configfile>`` block so
the same logic runs inside the foreign containers that the tools execute in
(they don't have this project installed). This test fails if anyone changes
``state.py`` without updating the inlined copies.
"""

import pathlib
import re

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "src" / "ndip_state" / "state.py"

TOOL_XMLS = [
    ROOT / "tools" / "reduction.xml",
    ROOT / "tools" / "simple_analyzer.xml",
    ROOT / "tools" / "data_assembler.xml",
]


def _extract_state_module(xml_text: str) -> str:
    m = re.search(
        r'<configfile name="state_module">#raw\n(.*?)\n#end raw</configfile>',
        xml_text,
        re.DOTALL,
    )
    assert m is not None, "no state_module configfile found in XML"
    return m.group(1)


@pytest.mark.parametrize("xml_path", TOOL_XMLS, ids=lambda p: p.name)
def test_inlined_state_module_matches_canonical(xml_path):
    canonical = CANONICAL.read_text()
    inlined = _extract_state_module(xml_path.read_text())
    assert inlined.rstrip() == canonical.rstrip(), (
        f"{xml_path.name}: inlined state_module diverged from "
        "src/ndip_state/state.py — re-sync the configfile."
    )
