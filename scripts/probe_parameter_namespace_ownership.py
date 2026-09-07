"""Reproduce the Insert parameter-namespace overwrite this repository fixed.

Run it: ``python scripts/probe_parameter_namespace_ownership.py``. It needs no
Fusion, no venv and no network -- the design is an in-memory stand-in for the
Fusion API, and nothing on disk or in Fusion is touched.

WHAT IT SHOWS. Before f22aa7c, ``insert`` chose its parameter namespace from
recorded link records rather than from the document's actual parameters.
``detach`` removes link attributes and never touches ``userParameters``, so a
detached namespace and the geometry it drives survive with no record; a fresh
Insert of that slug then saw the namespace as free and ``_push_parameters``
reassigned the surviving parameters with no ownership check. Against the code
of that era the probe prints an allocator result of ``wg_horn_`` and an
existing ``wg_horn_depth`` rewritten from 25 mm to 50 mm. Against current code
the allocator steps to a free namespace and the survivors are left alone.

WHY IT IS KEPT. The behaviour is covered by regression tests, so this script is
not a gate and nothing runs it automatically. It is kept because two days of
work on this path established that reasoning about it from reading alone is
unreliable: the defect was found by executing the real allocator and the real
push, not by inspecting them, and the same is likely to be true of the next
question asked about parameter ownership. Reach for it before arguing from the
source.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGLink"

adsk = types.ModuleType("adsk")
adsk.__path__ = []
adsk_core = types.ModuleType("adsk.core")
adsk_fusion = types.ModuleType("adsk.fusion")
adsk.core = adsk_core
adsk.fusion = adsk_fusion
sys.modules["adsk"] = adsk
sys.modules["adsk.core"] = adsk_core
sys.modules["adsk.fusion"] = adsk_fusion
sys.path.insert(0, str(ADDIN))

spec = importlib.util.spec_from_file_location("wglink_core_probe", ADDIN / "wglink_core.py")
core = importlib.util.module_from_spec(spec)
sys.modules["wglink_core_probe"] = core
spec.loader.exec_module(core)

from wglink_bundle import Bundle, LinkIdentity, instance_parameter_prefix  # noqa: E402

core.adsk.core.ValueInput = types.SimpleNamespace(createByString=lambda value: value)


def make_bundle(depth_mm: float) -> Bundle:
    manifest = {
        "design": {"name": "Horn", "build_mode": "freestanding"},
        "parameters": [
            {"name": "wg_horn_throat_dia", "unit": "mm", "value": 25.4, "role": "interface"},
            {"name": "wg_horn_depth", "unit": "mm", "value": depth_mm, "role": "interface"},
        ],
    }
    return Bundle(
        manifest=manifest,
        grid={},
        root=Path("."),
        source=Path("."),
        identity=LinkIdentity(
            design_id="d", lineage_id=None, export_id="e", export_sequence=1
        ),
    )


class Parameters:
    """Stand-in for design.userParameters."""

    def __init__(self, existing: dict[str, str]):
        self.values = {
            name: types.SimpleNamespace(name=name, expression=expression, unit="mm")
            for name, expression in existing.items()
        }
        self.added: list[str] = []

    @property
    def count(self):
        return len(self.values)

    def item(self, index):
        return list(self.values.values())[index]

    def itemByName(self, name):
        return self.values.get(name)

    def add(self, name, value, unit, description):
        parameter = types.SimpleNamespace(name=name, expression=value, unit=unit)
        self.values[name] = parameter
        self.added.append(name)
        return parameter


# --- The user's document AFTER Detach. -------------------------------------
# Detach only deletes attributes (wglink_core.detach), so no link records
# remain while the parameters and the geometry they drive survive.
records: dict[str, dict] = {}
stored = core._stored_parameter_prefixes(records)
held = ["wg_horn_depth", "wg_horn_throat_dia"]

print("link records remaining after Detach:", stored or "none")
print("parameters the document still holds:", held)
print()

# --- The old call: allocate from records alone. ----------------------------
# This is what Insert did before f22aa7c, and it is still what the allocator
# does when nobody hands it the document's parameters -- `parameter_names`
# defaults to (). Passing two arguments therefore reproduces the defect
# faithfully rather than by rigging it.
old_prefix = instance_parameter_prefix("horn", stored)
parameters = Parameters({"wg_horn_depth": "25 mm", "wg_horn_throat_dia": "25.4 mm"})
design = types.SimpleNamespace(userParameters=parameters)
before = parameters.values["wg_horn_depth"].expression
report = core._push_parameters(design, make_bundle(50.0), old_prefix)
after = parameters.values["wg_horn_depth"].expression

print("[allocating from records only -- the pre-f22aa7c call]")
print("  namespace chosen:   ", old_prefix)
print("  wg_horn_depth:      ", before, "->", after)
print("  push report:        ", report)
overwritten = before != after
print("  verdict:            ", "OVERWRITTEN -- this is the defect" if overwritten
      else "left alone")
print()

# --- The current call: allocate from the document too. ---------------------
# insert() passes _document_parameter_names(design) as the third argument.
new_prefix = instance_parameter_prefix("horn", stored, held)
fresh = Parameters({"wg_horn_depth": "25 mm", "wg_horn_throat_dia": "25.4 mm"})
fresh_design = types.SimpleNamespace(userParameters=fresh)
fresh_before = fresh.values["wg_horn_depth"].expression
fresh_report = core._push_parameters(fresh_design, make_bundle(50.0), new_prefix)
fresh_after = fresh.values["wg_horn_depth"].expression

print("[allocating from records AND the document -- what insert() does now]")
print("  namespace chosen:   ", new_prefix)
print("  wg_horn_depth:      ", fresh_before, "->", fresh_after)
print("  push report:        ", fresh_report)
survived = fresh_before == fresh_after
print("  verdict:            ", "left alone -- the survivor keeps its value" if survived
      else "OVERWRITTEN")
print()

if overwritten and survived and new_prefix != old_prefix:
    print("PASS: the defect reproduces on the old call and does not on the current one.")
else:
    print("UNEXPECTED: re-read this probe before trusting either result.")
    raise SystemExit(1)
