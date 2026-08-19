from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_docs_require_active_environment_packages():
    for rel_path in (
        "README.md",
        "docs/WGMETAL-PIPELINE-GUIDE.md",
        "docs/WGLINK-GUIDE.md",
        "requirements.txt",
    ):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        assert "active environment" in normalized
        assert "sibling" in normalized
        assert "../hornlab-" not in normalized


def test_dependency_pins_cover_required_solver_contracts():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert (
        "hornlab-metal-bem.git@328f981d2ad37642a68be97a7ea2e46cb7b14683"
        in requirements
    )
    assert (
        "hornlab-sim.git@d6a0c36da229eb7d5a71823052b60f0a82847646"
        in requirements
    )
    assert (
        "hornlab-plots.git@a15bcf5b7498dd60437ef5f567e852af6f270c0b"
        in requirements
    )
