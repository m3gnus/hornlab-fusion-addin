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
        "hornlab-sim.git@260155deac6c539b2e55bd8573b007da1114c004"
        in requirements
    )
    assert (
        "hornlab-plots.git@ff30cafbe1631012f2e69c378dfe1fcc27ec7e75"
        in requirements
    )
