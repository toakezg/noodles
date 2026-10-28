"""Load a result folder and build the data bundle for the viewer."""

import json
import os
import re
from pathlib import Path

from noodles.utils import _sanitize_id

VIEWER_THEMES = ("auto", "light", "dark")

# Code excerpts shown in the viewer are bounded per function to keep results small:
# the first MAX_SNIPPET_LINES lines, plus CALL_CONTEXT_LINES around each later
# call site, up to MAX_SNIPPET_TOTAL_LINES in all.
MAX_SNIPPET_LINES = 80
CALL_CONTEXT_LINES = 10
MAX_SNIPPET_TOTAL_LINES = 300
SNIPPETS_FILE = "snippets.json"


def _snippet_ranges(total: int, call_offsets: list[int]) -> list[tuple[int, int]]:
    """Line ranges [start, end) to keep: the head, plus a window around later calls."""
    ranges = [(0, min(total, MAX_SNIPPET_LINES))]
    for off in sorted(set(call_offsets)):
        if off < MAX_SNIPPET_LINES or off >= total:
            continue
        ranges.append((max(0, off - CALL_CONTEXT_LINES), min(total, off + CALL_CONTEXT_LINES + 1)))

    merged: list[tuple[int, int]] = []
    budget = MAX_SNIPPET_TOTAL_LINES
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            start = merged[-1][1]
            if end <= start:
                continue
            take = min(end - start, budget)
            merged[-1] = (merged[-1][0], start + take)
        else:
            take = min(end - start, budget)
            merged.append((start, start + take))
        budget -= take
        if budget <= 0:
            break
    return merged


def write_snippets(call_graph: dict, result_dir: str | Path) -> Path:
    """Save a bounded source excerpt per function for the viewer's code panel.

    Kept out of call_graph.json, which stays free of source code (it is also
    returned verbatim by the MCP tools).

    Writes {function_id: {"line": first_line, "total_lines": n,
                          "segments": [{"offset": i, "code": text}, ...]}},
    where offset is the segment's first line relative to the function's first line.
    """
    call_lines: dict[str, list[int]] = {}
    for edge in call_graph.get("edges", []):
        if edge.get("line"):
            call_lines.setdefault(edge["from"], []).append(edge["line"])

    snippets: dict[str, dict] = {}
    for node in call_graph["nodes"]:
        source = node.get("source") or ""
        if not source or node.get("line") is None:
            continue
        lines = source.splitlines()
        # The definition's text starts at its column; restore that indent on line 1
        lines[0] = " " * (node.get("column") or 0) + lines[0]
        offsets = [ln - node["line"] for ln in call_lines.get(node["id"], [])]
        snippets[node["id"]] = {
            "line": node["line"],
            "total_lines": len(lines),
            "segments": [
                {"offset": start, "code": "\n".join(lines[start:end])}
                for start, end in _snippet_ranges(len(lines), offsets)
            ],
        }
    path = Path(result_dir) / SNIPPETS_FILE
    path.write_text(json.dumps(snippets), encoding="utf-8")
    return path


def _load_snippets(result_path: Path) -> dict:
    path = result_path / SNIPPETS_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve_theme(theme: str | None = None) -> str:
    """Default viewer theme: the given value, else $NOODLES_THEME, else "auto" (follow the OS)."""
    value = (theme or os.environ.get("NOODLES_THEME") or "auto").strip().lower()
    return value if value in VIEWER_THEMES else "auto"


def load_result(result_dir: str | Path, theme: str | None = None) -> dict:
    """Load a result folder and return the viewer data bundle.

    Args:
        result_dir: Path to the result folder containing call_graph.json
                    and diagram_*.mmd files.
        theme: Default viewer theme ("auto", "light" or "dark"). Defaults to
               $NOODLES_THEME, else "auto". A theme chosen in the viewer wins.

    Returns:
        Dict with keys: diagrams, navigation, nodes, edges, id_map, theme,
        snippets (empty for results made before snippets.json existed)
    """
    result_path = Path(result_dir)

    # Load call graph
    cg_path = result_path / "call_graph.json"
    with open(cg_path, encoding="utf-8") as f:
        call_graph = json.load(f)

    # Load all diagram files
    diagrams: dict[str, str] = {}
    for mmd_file in sorted(result_path.glob("diagram_*.mmd")):
        # "diagram_main.mmd" -> "main", "diagram_Execute_CLI_tool.mmd" -> "Execute_CLI_tool"
        name = mmd_file.stem.removeprefix("diagram_")
        diagrams[name] = mmd_file.read_text(encoding="utf-8")

    # Build id_map: sanitized_mermaid_id -> original_node_id
    id_map: dict[str, str] = {}
    for node in call_graph["nodes"]:
        sanitized = _sanitize_id(node["id"])
        id_map[sanitized] = node["id"]

    # Build nodes metadata lookup: original_id -> metadata
    nodes: dict[str, dict] = {}
    for node in call_graph["nodes"]:
        nid = node["id"]
        # Extract file path and function name from the id
        parts = nid.split("::")
        file_path = parts[0] if len(parts) > 1 else ""
        func_name = parts[-1]

        nodes[nid] = {
            "name": node.get("name", func_name),
            "description": node.get("description", ""),
            "type": node.get("type", "process"),
            "tag": node.get("tag", ""),
            "file_path": file_path,
            "function": func_name,
            "status": node.get("status", "unchanged"),
            "update": node.get("update", ""),
            "line": node.get("line"),
        }

    # Build edges metadata lookup: "sanitized_from -> sanitized_to" -> metadata
    edges: dict[str, dict] = {}
    for edge in call_graph["edges"]:
        key = f"{_sanitize_id(edge['from'])} -> {_sanitize_id(edge['to'])}"
        edges[key] = {
            "label": edge.get("label", ""),
            "description": edge.get("description", ""),
            "args": edge.get("args", ""),
            "condition": edge.get("condition"),
            "is_returned": edge.get("is_returned", False),
            "index": edge.get("index"),
            "from": edge["from"],
            "to": edge["to"],
            "line": edge.get("line"),
        }

    # Build navigation map: for each diagram, which nodes link to which sub-diagrams
    # A node is clickable if a sub-diagram file exists matching its sanitized name
    available_diagrams = set(diagrams.keys())
    mermaid_id_to_sub: dict[str, str] = {}
    for node in call_graph["nodes"]:
        # Extract function name from node ID (e.g., "src/foo.py::my_func" -> "my_func")
        # or use the explicit name field if present
        name = node.get("name", "")
        if not name:
            node_id = node.get("id", "")
            if "::" in node_id:
                name = node_id.split("::")[-1]
        if not name:
            continue
        sanitized_name = _sanitize_id(name)
        mermaid_id = _sanitize_id(node["id"])
        if sanitized_name in available_diagrams:
            mermaid_id_to_sub[mermaid_id] = sanitized_name
        else:
            for i in range(2, 10):
                candidate = f"{sanitized_name}_{i}"
                if candidate in available_diagrams:
                    mermaid_id_to_sub[mermaid_id] = candidate
                    break

    # Parse each diagram to extract node IDs and build nav links
    navigation: dict[str, dict[str, str]] = {}
    for diagram_name, source in diagrams.items():
        nav: dict[str, str] = {}
        for line in source.split("\n"):
            stripped = line.strip()
            # Node definition: ID followed by shape bracket or brace
            match = re.match(r"(\w+)\s*[\(\[\{\"']", stripped)
            if not match:
                continue
            mermaid_id = match.group(1)
            if mermaid_id in mermaid_id_to_sub:
                target = mermaid_id_to_sub[mermaid_id]
                if target != diagram_name:  # Don't allow self-references
                    nav[mermaid_id] = target
        if nav:
            navigation[diagram_name] = nav

    return {
        "diagrams": diagrams,
        "navigation": navigation,
        "nodes": nodes,
        "edges": edges,
        "id_map": id_map,
        "theme": resolve_theme(theme),
        "snippets": _load_snippets(result_path),
    }


def write_viewer_files(result_dir: str | Path) -> None:
    """Generate and write viewer bundle and HTML files.

    Args:
        result_dir: Path to the result folder containing call_graph.json
                    and diagram_*.mmd files.

    Writes:
        - viewer_bundle.json: JSON data bundle for the viewer
        - viewer.html: Self-contained HTML viewer with embedded data
    """
    result_path = Path(result_dir)

    print("Generating viewer bundle ...")
    viewer_bundle = load_result(result_path)
    viewer_bundle_file = result_path / "viewer_bundle.json"
    viewer_bundle_file.write_text(json.dumps(viewer_bundle))
    print(f"  Viewer bundle: {viewer_bundle_file}")

    viewer_html = generate_viewer_html(result_path)
    viewer_html_file = result_path / "viewer.html"
    # Explicit UTF-8: the page declares UTF-8, and Windows' default encoding
    # can't represent every character in descriptions/source
    viewer_html_file.write_text(viewer_html, encoding="utf-8")
    print(f"  Viewer HTML:   {viewer_html_file}")


def generate_viewer_html(result_dir: str | Path, theme: str | None = None) -> str:
    """Generate a self-contained HTML viewer with embedded data.

    Args:
        result_dir: Path to the result folder containing call_graph.json
                    and diagram_*.mmd files.
        theme: Default viewer theme ("auto", "light" or "dark"), see load_result.

    Returns:
        Complete HTML string that can be saved as viewer.html.
    """
    bundle = load_result(result_dir, theme=theme)
    template_path = Path(__file__).parent / "static/index.html"
    template = template_path.read_text(encoding="utf-8")

    # Replace the fetch() block with inline data
    # Original pattern fetches from /api/data, we replace with embedded JSON
    fetch_block = """fetch('/api/data')
    .then(r => r.json())
    .then(data => {
      DATA = data;
      loading.style.display = 'none';
      navigateTo('main', 'Main');
    })
    .catch(err => {
      loading.textContent = 'Failed to load data: ' + err.message;
    });"""

    # Use setTimeout to defer execution until after all variables are declared
    # (the original fetch was async so this wasn't needed)
    inline_code = f"""DATA = {json.dumps(bundle)};
  setTimeout(() => {{
    loading.style.display = 'none';
    navigateTo('main', 'Main');
  }}, 0);"""

    html = template.replace(fetch_block, inline_code)
    return html
