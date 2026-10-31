"""HTML wrapper for the locally bundled Three.js dashboard component."""

from __future__ import annotations

from html import escape
from pathlib import Path

STAGES = (
    ("Movie Review", "Movie Review Input"),
    ("Preprocessing", "Text Preprocessing"),
    ("TF-IDF / Tokens", "TF-IDF / Tokenization"),
    ("Inference", "Model Inference"),
    ("Sentiment", "Sentiment Output"),
)


def build_scene_html(bundle_path: str | Path) -> str:
    """Return the isolated scene iframe markup, with a no-build fallback."""
    path = Path(bundle_path)
    css_path = path.with_name("scene.css")
    if not path.is_file() or not css_path.is_file():
        return (
            '<div class="scene-fallback" role="status">'
            "The 3D scene bundle is not available yet. Build it with "
            "<code>npm run --prefix frontend build</code>."
            "</div>"
        )

    bundle = path.read_text(encoding="utf-8").replace("</script", "<\\/script")
    stylesheet = css_path.read_text(encoding="utf-8")
    stage_buttons = "".join(
        f'<button class="stage-button" data-stage="{index}" '
        f'aria-pressed="{str(index == 0).lower()}">{escape(label)}</button>'
        for index, (label, _) in enumerate(STAGES)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>{stylesheet}</style>
</head>
<body>
  <section class="scene-shell" aria-label="Interactive sentiment analysis pipeline">
    <div id="scene-canvas" aria-label="Three-dimensional movie review pipeline"></div>
    <div class="stage-row" role="group" aria-label="Pipeline stages">
      {stage_buttons}
    </div>
    <div id="stage-note" aria-live="polite">
      {escape(STAGES[0][1])}: A raw IMDb review enters the sentiment workflow.
    </div>
  </section>
  <script>{bundle}</script>
</body>
</html>"""
