"""Prove the suite is measuring the hornlab-waveguide-mesher revision we pin.

The add-in is a thin caller: most of what these tests assert lives in
``hornlab_mesher``, pinned to an exact commit in ``requirements.txt``. Two
different mechanisms load that package, and neither of them is guaranteed to
be the pin:

* ``hornlab_mesher`` is imported from wherever ``sys.path`` finds it. A
  development venv here carries
  ``site-packages/hornlab_waveguide_mesher_local.pth``, one line doing
  ``sys.path.insert(0, "<workspace>/hornlab-waveguide-mesher")``. It runs at
  interpreter start and inserts at index 0, so it beats both the installed
  package and ``PYTHONPATH`` -- silently, and with no error.
* ``tests/test_wg_mesh_sizing.py`` loads ``mesh_sizing.py`` by path from
  ``<repo>/../hornlab-waveguide-mesher``, which need not be the same tree.

So a run can import one revision, read a second by path, and report a
red/green that belongs to neither the pin nor anything the product ships.
That has happened here more than once, and it always looks like a defect in
the add-in rather than an environment fault. CI proves the pin resolves (see
the "Prove the pinned mesher is the one that imports" step in
``.github/workflows/ci.yml``); this file is the same proof for a local run,
which is where the mistake is actually made.

A checkout *ahead* of the pin passes -- developing the two repositories
together is the normal reason to have that ``.pth`` -- but a checkout that
does not contain the pin at all is stale, and stops the run. An installed
copy is judged on the exact revision pip recorded for it, and an install made
from a local directory records none, so it can only be reported, not checked.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
SIBLING_MESHER = ROOT.parent / "hornlab-waveguide-mesher"

_PIN_PATTERN = re.compile(r"hornlab-waveguide-mesher\.git@([0-9a-f]{40})")


def _declared_pin() -> str | None:
    try:
        text = REQUIREMENTS.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _PIN_PATTERN.search(text)
    return match.group(1) if match else None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ("git", "-C", str(repo), *args),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _mesher_checkout(tree: Path) -> Path | None:
    """The mesher git checkout ``tree`` belongs to, or None if it is not one.

    An installed copy under ``site-packages`` is not a checkout -- and because
    ``.venv`` lives inside this repository, asking git about it answers about
    *this* repository, so the toplevel has to be confirmed to be a mesher.
    """
    result = _git(tree, "rev-parse", "--show-toplevel")
    if result is None or result.returncode != 0:
        return None
    toplevel = Path(result.stdout.strip())
    if not (toplevel / "hornlab_mesher" / "step_import.py").exists():
        return None
    return toplevel


def _contains(checkout: Path, commit: str) -> bool | None:
    """Whether ``commit`` is reachable from HEAD. None when git cannot say.

    ``--is-ancestor`` exits 0 for yes and 1 for no; anything else (an unknown
    object in a shallow clone, say) is not an answer and must not be read as
    one.
    """
    result = _git(checkout, "merge-base", "--is-ancestor", commit, "HEAD")
    if result is None or result.returncode > 1:
        return None
    return result.returncode == 0


def _head(checkout: Path) -> str:
    result = _git(checkout, "rev-parse", "--short", "HEAD")
    if result is None or result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def _installed_revision(imported: Path) -> str | None:
    """The commit an installed (non-checkout) ``hornlab_mesher`` was built from.

    ``pip install git+...@<sha>`` records it in PEP 610 ``direct_url.json``. An
    install made from a local directory instead records no revision at all, so
    None here means "cannot be established", never "matches".
    """
    try:
        dist = importlib.metadata.distribution("hornlab-waveguide-mesher")
        located = Path(str(dist.locate_file("hornlab_mesher"))).resolve()
        if located != imported:
            return None  # metadata for a copy that is not the one importing
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - absent metadata is not an answer either
        return None
    if not raw:
        return None
    try:
        return json.loads(raw).get("vcs_info", {}).get("commit_id")
    except (ValueError, AttributeError):
        return None


def _imported_mesher() -> Path | None:
    try:
        import hornlab_mesher
    except Exception:  # noqa: BLE001 - absent or broken is the tests' story to tell
        return None
    location = getattr(hornlab_mesher, "__file__", None)
    return Path(location).resolve().parent if location else None


def _stale(label: str, tree: Path | None, pin: str) -> str | None:
    """A one-paragraph complaint about ``tree``, or None if it is fine."""
    if tree is None or not tree.exists():
        return None
    checkout = _mesher_checkout(tree)
    if checkout is None:
        installed = _installed_revision(tree)
        if installed is None or installed == pin:
            return None
        return (
            f"  {label}\n"
            f"    {tree}\n"
            f"    installed from {installed[:8]}, which is not the pin"
        )
    if _contains(checkout, pin) is not False:
        return None
    return (
        f"  {label}\n"
        f"    {checkout}\n"
        f"    HEAD {_head(checkout)} does not contain the pin"
    )


def pytest_report_header() -> list[str]:
    pin = _declared_pin()
    imported = _imported_mesher()
    lines = [f"hornlab-waveguide-mesher pin: {pin or 'not declared'}"]
    if imported is None:
        lines.append("hornlab_mesher: not importable")
        return lines
    checkout = _mesher_checkout(imported)
    if checkout is not None:
        where = f"checkout HEAD {_head(checkout)}"
    else:
        installed = _installed_revision(imported)
        where = (
            f"installed from {installed[:8]}"
            if installed
            else "installed, revision not recorded"
        )
    lines.append(f"hornlab_mesher: {imported} ({where})")
    return lines


def pytest_configure(config: pytest.Config) -> None:
    pin = _declared_pin()
    if pin is None:
        return
    problems = [
        problem
        for problem in (
            _stale("imported by hornlab_mesher", _imported_mesher(), pin),
            _stale("read by path from tests/test_wg_mesh_sizing.py", SIBLING_MESHER, pin),
        )
        if problem is not None
    ]
    if not problems:
        return
    raise pytest.UsageError(
        "this environment would measure a hornlab-waveguide-mesher that is not "
        f"the revision requirements.txt pins ({pin[:8]}), so a red or a green "
        "from it would be about neither the pin nor anything the product "
        "ships:\n"
        + "\n".join(problems)
        + "\n\n"
        "Update the checkout(s) named above to a revision that contains the "
        "pin, or build a venv from requirements.txt alone and remove "
        "site-packages/hornlab_waveguide_mesher_local.pth. See this file's "
        "docstring for why the obvious PYTHONPATH override does not work."
    )
