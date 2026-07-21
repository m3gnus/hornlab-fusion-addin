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
        "hornlab-metal-bem.git@09f3b0b0e99b23936cac31531b1f82c6e369ea44"
        in requirements
    )
    assert (
        "hornlab-sim.git@764e94fc49619193c8737da83c35b684a5ccfec6"
        in requirements
    )
    assert (
        "hornlab-plots.git@916ed784bb026838f47a380c542638da32080fa3"
        in requirements
    )
