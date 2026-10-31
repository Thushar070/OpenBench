from deeptext.dashboard.scene import build_scene_html


def test_scene_html_has_an_accessible_no_bundle_fallback(tmp_path) -> None:
    html = build_scene_html(tmp_path / "missing.js")

    assert 'role="status"' in html
    assert "3D scene bundle is not available" in html
    assert "npm run --prefix frontend build" in html


def test_scene_html_includes_local_bundle_and_stage_controls(tmp_path) -> None:
    bundle = tmp_path / "three_scene.js"
    bundle.write_text("window.sceneReady = true;", encoding="utf-8")
    bundle.with_name("scene.css").write_text("body { color: green; }", encoding="utf-8")

    html = build_scene_html(bundle)

    assert "window.sceneReady = true;" in html
    assert "body { color: green; }" in html
    assert 'aria-label="Pipeline stages"' in html
    assert 'data-stage="4"' in html
    assert "Sentiment" in html
