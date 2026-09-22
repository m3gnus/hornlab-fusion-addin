from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wglink_install_docs_reference_current_installer():
    for rel_path in ("README.md", "docs/WGLINK-GUIDE.md", "fusion-addins/WGLink/README.md"):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        assert "scripts/install_wglink_addin.py" in text
        assert "scripts/install_fusion_wg_metal_addin.py" not in text


def test_pipeline_guides_do_not_offer_removed_commands():
    for rel_path in ("docs/HEADLESS.md", "docs/WGMETAL-PIPELINE-GUIDE.md"):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        assert "python scripts/" not in text
        assert "WGLink" in text
