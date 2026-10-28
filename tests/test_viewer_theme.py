"""Tests for the viewer's theme default and the generated viewer.html."""

import json
import tempfile
from pathlib import Path

import pytest

from noodles.viewer.data_loader import (
    generate_viewer_html,
    load_result,
    resolve_theme,
    write_viewer_files,
)


def _write_result(result_dir: Path, description: str = "Adds two numbers") -> None:
    call_graph = {
        "nodes": [
            {"id": "app.py::main", "type": "start_point", "callers": [], "callees": ["app.py::add"]},
            {"id": "app.py::add", "type": "end_point", "callers": ["app.py::main"], "callees": [],
             "description": description},
        ],
        "edges": [{"from": "app.py::main", "to": "app.py::add", "is_returned": True, "index": 0}],
    }
    (result_dir / "call_graph.json").write_text(json.dumps(call_graph), encoding="utf-8")
    (result_dir / "diagram_main.mmd").write_text(
        "graph LR\n"
        "    classDef start fill:none,stroke:#4db6ac,color:#00695c\n"
        '    app_py_main{{"app:main"}}:::start\n'
        '    app_py_add["app:add"]\n'
        "    app_py_main --> app_py_add\n",
        encoding="utf-8",
    )


class TestResolveTheme:
    def test_default_is_auto(self, monkeypatch):
        monkeypatch.delenv("NOODLES_THEME", raising=False)
        assert resolve_theme() == "auto"

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("NOODLES_THEME", "Dark")
        assert resolve_theme() == "dark"

    def test_explicit_beats_env(self, monkeypatch):
        monkeypatch.setenv("NOODLES_THEME", "dark")
        assert resolve_theme("light") == "light"

    @pytest.mark.parametrize("value", ["purple", "", "  "])
    def test_invalid_falls_back_to_auto(self, monkeypatch, value):
        monkeypatch.setenv("NOODLES_THEME", value)
        assert resolve_theme() == "auto"


class TestViewerBundle:
    def test_bundle_carries_theme(self, monkeypatch):
        monkeypatch.setenv("NOODLES_THEME", "dark")
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_result(Path(tmpdir))
            assert load_result(tmpdir)["theme"] == "dark"
            assert load_result(tmpdir, theme="light")["theme"] == "light"

    def test_generated_html_inlines_data(self, monkeypatch):
        """The template's fetch('/api/data') block must still be found and replaced."""
        monkeypatch.setenv("NOODLES_THEME", "dark")
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_result(Path(tmpdir))
            html = generate_viewer_html(tmpdir)
        assert "fetch('/api/data')" not in html
        assert "DATA = {" in html
        assert '"theme": "dark"' in html
        assert 'id="theme-btn"' in html

    def test_viewer_html_written_as_utf8(self):
        """Descriptions with non-ASCII text must not break viewer.html on Windows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_result(Path(tmpdir), description="Adds two numbers ✓ → 数字")
            write_viewer_files(tmpdir)
            html = (Path(tmpdir) / "viewer.html").read_text(encoding="utf-8")
        assert "\\u2713" in html or "✓" in html
