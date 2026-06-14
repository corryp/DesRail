"""
Renders conflict graph DOT files exported by DesRailDL.

Usage:
    python draw_conflict_graph.py                          # renders all output/conflict_graph_*.dot
    python draw_conflict_graph.py path/to/graph.dot        # renders a specific file
    python draw_conflict_graph.py path/to/*.dot            # renders multiple files

Requires: pip install graphviz
(Also requires Graphviz system install: https://graphviz.org/download/)
"""

import sys
import glob
import os

try:
    import graphviz
except ImportError:
    print("Missing 'graphviz' package. Install with: pip install graphviz")
    print("Also need Graphviz system install: https://graphviz.org/download/")
    sys.exit(1)


def render_dot(dot_path, fmt="png"):
    """Render a .dot file to an image."""
    out_path = dot_path.replace(".dot", "")
    src = graphviz.Source.from_file(dot_path)
    src.format = fmt
    rendered = src.render(out_path, cleanup=True)
    print(f"  {dot_path} -> {rendered}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        files = sys.argv[1:]
    else:
        # Default: look for DOT files in ../output/
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(script_dir, "..", "DesRail", "output")
        files = sorted(glob.glob(os.path.join(output_dir, "conflict_graph_*.dot")))
        if not files:
            print(f"No conflict_graph_*.dot files found in {output_dir}")
            sys.exit(1)

    print(f"Rendering {len(files)} graph(s):")
    for f in files:
        render_dot(f)
    print("Done.")
