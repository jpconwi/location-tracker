"""
views/page_view.py — Serves HTML templates (View layer)
"""
import os
from fastapi.responses import HTMLResponse


def render(template_name: str) -> HTMLResponse:
    """Read and return an HTML file from the templates directory."""
    # This file lives at backend/views/page_view.py
    # Templates live at frontend/templates/
    _this_dir = os.path.dirname(os.path.abspath(__file__))   # backend/views/
    _backend  = os.path.dirname(_this_dir)                    # backend/
    _root     = os.path.dirname(_backend)                     # repo root
    path = os.path.join(_root, "frontend", "templates", template_name)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)
