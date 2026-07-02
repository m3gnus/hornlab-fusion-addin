from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_docs_match_workspace_discovery_order():
    for rel_path in ("README.md", "docs/USER-GUIDE.md", "requirements.txt"):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        assert "top-level workspace siblings" in normalized
        assert "legacy" in normalized
        assert "installed" in normalized
