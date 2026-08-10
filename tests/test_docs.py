from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_docs_require_active_environment_packages():
    for rel_path in ("README.md", "docs/USER-GUIDE.md", "requirements.txt"):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        assert "active environment" in normalized
        assert "sibling" in normalized
        assert "../hornlab-" not in normalized


def test_dependency_pins_cover_required_solver_contracts():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert (
        "hornlab-metal-bem.git@469def27469bef411807be95539f8376d13177f6"
        in requirements
    )
    assert (
        "hornlab-sim.git@dbc22732f51925393543c00987f54386fc64aecf"
        in requirements
    )
    assert (
        "hornlab-plots.git@ea7c94f4d43672745455df97610b65af05c76348"
        in requirements
    )
