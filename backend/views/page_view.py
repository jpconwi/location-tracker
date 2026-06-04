"""
views/page_view.py — Serves HTML templates (View layer)
"""
import os
from fastapi.responses import HTMLResponse


def render(template_name: str) -> HTMLResponse:
    """Read and return an HTML file from the templates directory."""
    base = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(base, "frontend", "templates", template_name)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)
