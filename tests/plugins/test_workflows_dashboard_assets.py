from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "plugins" / "workflows" / "dashboard" / "dist" / "index.js"


def test_workflow_dashboard_renders_assistant_error_hints() -> None:
    text = BUNDLE.read_text(encoding="utf-8")
    assert "workflow_assistant_runtime_error" in text
    assert "Check workflow assistant provider/model configuration" in text
    assert "Advanced YAML" in text


def test_workflow_dashboard_contains_privacy_warning() -> None:
    text = BUNDLE.read_text(encoding="utf-8")
    assert "Workflow inputs and outputs are stored locally" in text
    assert "Do not paste secrets" in text


def test_workflow_dashboard_has_accessible_cell_editor_path() -> None:
    text = BUNDLE.read_text(encoding="utf-8")
    assert "Workflow cell list" in text
    assert "Edit cell" in text
    assert "aria-label" in text


def test_workflow_dashboard_has_responsive_editor_css() -> None:
    css = (ROOT / "plugins" / "workflows" / "dashboard" / "dist" / "style.css").read_text(encoding="utf-8")
    assert "hermes-workflows-editor-layout" in css
    assert "@media" in css
    assert "react-flow__minimap" in css or "hermes-workflows-minimap" in css
