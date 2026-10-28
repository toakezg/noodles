"""Tests for call-site line numbers and the viewer's code excerpts (snippets.json)."""

import asyncio
import json
import tempfile
from pathlib import Path

from noodles.agents.repo_analyzer.call_graph_builder.call_graph_builder import (
    build_call_graph,
)
from noodles.agents.repo_analyzer.repo_analyzer import analyze_local_repo
from noodles.viewer.data_loader import (
    CALL_CONTEXT_LINES,
    MAX_SNIPPET_LINES,
    MAX_SNIPPET_TOTAL_LINES,
    _snippet_ranges,
    load_result,
    write_snippets,
)

APP_JS = """function helper() {
  return 1;
}

function main() {
  const a = 1;
  // a comment
  return helper();
}
"""

SHAPES_PY = """class Shape:
    def area(self):
        return 1

    def describe(self):
        x = 1
        return self.area()
"""


def _repo(files: dict[str, str]) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    for rel, content in files.items():
        (Path(tmp.name) / rel).write_text(content, encoding="utf-8")
    return tmp


class TestCallLines:
    def test_edge_records_call_site_line(self):
        with _repo({"app.js": APP_JS}) as repo:
            call_graph, _, _, _ = build_call_graph(Path(repo))
        edge = next(e for e in call_graph["edges"] if e["from"].endswith("::main"))
        assert edge["line"] == 8

    def test_node_records_column(self):
        with _repo({"shapes.py": SHAPES_PY}) as repo:
            call_graph, _, _, _ = build_call_graph(Path(repo))
        node = next(n for n in call_graph["nodes"] if n["id"].endswith("Shape.describe"))
        assert node["line"] == 5
        assert node["column"] == 4


class TestWriteSnippets:
    def test_restores_first_line_indent(self):
        with _repo({"shapes.py": SHAPES_PY}) as repo:
            call_graph, _, _, _ = build_call_graph(Path(repo))
            write_snippets(call_graph, repo)
            snippets = json.loads((Path(repo) / "snippets.json").read_text(encoding="utf-8"))
        snip = next(v for k, v in snippets.items() if k.endswith("Shape.describe"))
        assert snip["line"] == 5
        code = snip["segments"][0]["code"].splitlines()
        assert code[0] == "    def describe(self):"
        assert code[2] == "        return self.area()"

    def test_long_function_keeps_head_and_window_around_late_call(self):
        """A call far down a long function is still viewable with context around it."""
        body = "".join(f"    x{i} = {i}\n" for i in range(200))
        code = "def helper():\n    return 1\n\ndef big():\n" + body + "    helper()\n"
        with _repo({"big.py": code}) as repo:
            call_graph, _, _, _ = build_call_graph(Path(repo))
            write_snippets(call_graph, repo)
            snippets = json.loads((Path(repo) / "snippets.json").read_text(encoding="utf-8"))
        snip = next(v for k, v in snippets.items() if k.endswith("::big"))
        assert snip["total_lines"] == 202
        head, window = snip["segments"]
        assert head["offset"] == 0 and len(head["code"].splitlines()) == MAX_SNIPPET_LINES
        # helper() is the function's last line (offset 201); the window ends there
        assert window["offset"] == 201 - CALL_CONTEXT_LINES
        assert window["code"].splitlines()[-1] == "    helper()"


class TestSnippetRanges:
    def test_short_function_is_whole(self):
        assert _snippet_ranges(30, [5, 20]) == [(0, 30)]

    def test_nearby_windows_merge(self):
        assert _snippet_ranges(500, [100, 105]) == [(0, 80), (90, 116)]

    def test_window_touching_head_extends_it(self):
        assert _snippet_ranges(500, [85]) == [(0, 96)]

    def test_total_is_capped(self):
        ranges = _snippet_ranges(5000, list(range(100, 5000, 50)))
        assert sum(end - start for start, end in ranges) == MAX_SNIPPET_TOTAL_LINES


class TestPipeline:
    def test_local_analysis_writes_snippets_but_keeps_call_graph_clean(self):
        with _repo({"app.js": APP_JS}) as repo, tempfile.TemporaryDirectory() as out:
            asyncio.run(analyze_local_repo(Path(repo), Path(out), enrich=False))
            call_graph = json.loads((Path(out) / "call_graph.json").read_text())
            assert all("source" not in n and "column" not in n for n in call_graph["nodes"])
            assert (Path(out) / "snippets.json").is_file()

            bundle = load_result(out)
            main_id = next(i for i in bundle["nodes"] if i.endswith("::main"))
            assert bundle["nodes"][main_id]["line"] == 5
            assert bundle["snippets"][main_id]["segments"][0]["code"].startswith("function main()")
            edge = next(e for e in bundle["edges"].values() if e["from"] == main_id)
            assert edge["line"] == 8

    def test_old_results_without_snippets_still_load(self):
        with _repo({"app.js": APP_JS}) as repo, tempfile.TemporaryDirectory() as out:
            asyncio.run(analyze_local_repo(Path(repo), Path(out), enrich=False))
            (Path(out) / "snippets.json").unlink()
            assert load_result(out)["snippets"] == {}
