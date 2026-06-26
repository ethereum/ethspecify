#!/usr/bin/env python3
"""
Extract a source location map from a consensus-specs checkout.

Scans the markdown spec files for Python code blocks and table rows, mapping
each spec item (function, class, constant, type) to its file path and line
range. Combined with the checkout's commit, this lets ethspecify build links
directly to the source on GitHub.

Output JSON shape:

    {
      "commit": "<consensus-specs HEAD sha>",
      "items": {
        "<item_name>": {
          "<fork>": {"file": "specs/<fork>/<file>.md", "start": <int>, "end": <int>}
        }
      }
    }

Line numbers are 1-indexed and refer to the raw markdown file (i.e. they line up
with GitHub's `?plain=1#L<start>-L<end>` anchors).

Usage:
    generate_links.py <consensus-specs-dir> [output-file]
"""

import ast
import json
import os
import re
import subprocess
import sys

# Table rows naming a constant/type, e.g. | `NAME` | ... |. The name may be
# followed by an annotation before the next cell, e.g. | `NAME` *deprecated* | ...
TABLE_ROW_RE = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`[^|]*\|")

# A list-of-records config var, defined by a comment above its table, e.g.
# <!-- list-of-records:blob_schedule --> -> BLOB_SCHEDULE
LIST_OF_RECORDS_RE = re.compile(r"<!--\s*list-of-records:\s*([A-Za-z0-9_-]+)\s*-->")

# Directive telling the spec parser to ignore the next element (table/code).
# We honor it too so reference tables aren't mistaken for definitions.
SKIP_RE = re.compile(r"<!--\s*eth_consensus_specs:\s*skip\s*-->")

# Markdown heading, used to track the current section.
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")

# Level-1/2 section headings that contain canonical definitions. A table row
# under one of these is treated as a definition (and so takes precedence over,
# e.g., a fork-version reference table under a "networking" section).
DEF_SECTIONS = {
    "constant",
    "constants",
    "config",
    "configuration",
    "preset",
    "presets",
    "type",
    "types",
    "custom type",
    "custom types",
}

# ALL_CAPS_WITH_UNDERSCORES constant/preset/config names
CONSTANT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")

# PascalCase type/container names
PASCAL_CASE_RE = re.compile(r"^[A-Z][a-zA-Z0-9]+$")

# Spec files that live outside specs/<fork>/ but contribute items to a fork.
# Mirrors consensus-specs' pysetup EXTRA_SPEC_FILES mapping.
EXTRA_FILES = {
    "sync/optimistic.md": "bellatrix",
}

# Spec files the parser ignores; mirrors consensus-specs' IGNORE_SPEC_FILES.
IGNORE_FILES = {
    "specs/phase0/deposit-contract.md",
}


def get_commit(repo_root):
    """Return the HEAD commit sha of the checkout, or None if unavailable."""
    try:
        return (
            subprocess.check_output(
                ["git", "-C", repo_root, "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, OSError):
        return None


def find_fork_directories(repo_root):
    """
    Find all fork/feature directories containing markdown spec files.

    Returns a list of (fork_name, directory_path) tuples.
    """
    specs_dir = os.path.join(repo_root, "specs")
    if not os.path.isdir(specs_dir):
        return []

    results = []

    # Main fork directories: specs/{fork}/
    for entry in sorted(os.listdir(specs_dir)):
        entry_path = os.path.join(specs_dir, entry)
        if os.path.isdir(entry_path) and not entry.startswith("_") and not entry.startswith("."):
            results.append((entry, entry_path))

    # Feature directories: specs/_features/{fork}/
    features_dir = os.path.join(specs_dir, "_features")
    if os.path.isdir(features_dir):
        for entry in sorted(os.listdir(features_dir)):
            entry_path = os.path.join(features_dir, entry)
            if os.path.isdir(entry_path) and not entry.startswith("."):
                results.append((entry, entry_path))

    return results


def find_markdown_files(directory):
    """Find all .md files recursively in a directory (deterministic order)."""
    md_files = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        filenames.sort()
        for filename in filenames:
            if filename.endswith(".md"):
                md_files.append(os.path.join(dirpath, filename))
    return md_files


def extract_from_file(filepath):
    """
    Extract spec items from a single markdown file.

    Returns a list of (item_name, start_line, end_line, kind) tuples, where line
    numbers are 1-indexed and kind is one of:
      - "code":      a code-block definition (function/class)
      - "lor":       a list-of-records config var
      - "table_def": a table row under a definition section
      - "ref":       a table row elsewhere (an incidental reference)
    """
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    items = []
    in_python_block = False
    code_block_start = 0  # 1-indexed line of the ```python marker
    code_lines = []
    section = None  # current level-1/2 heading (lowercased)

    # list-of-records state: a pending name, and the span of its table.
    lor_name = None
    lor_start = None
    lor_end = None

    # skip-directive state: the spec parser ignores the next element after a
    # `<!-- eth_consensus_specs: skip -->` comment, so we do too. skip_pending
    # means we're between the comment and its element; skip_mode is the element
    # being consumed ("table" or "fence").
    skip_pending = False
    skip_mode = None

    def flush_lor():
        nonlocal lor_name, lor_start, lor_end
        if lor_name is not None and lor_start is not None:
            items.append((lor_name, lor_start, lor_end, "lor"))
        lor_name = None
        lor_start = None
        lor_end = None

    for line_num_0, line in enumerate(lines):
        line_num = line_num_0 + 1  # 1-indexed
        stripped = line.rstrip()

        # Consuming a skipped element.
        if skip_mode == "table":
            if stripped.startswith("|"):
                continue
            skip_mode = None  # table ended; process this line normally
        elif skip_mode == "fence":
            if stripped == "```" or stripped.startswith("```"):
                skip_mode = None
            continue

        # A skip directive: ignore the next element (table or fenced code).
        if SKIP_RE.search(stripped):
            skip_pending = True
            continue

        # Decide what element the pending skip applies to.
        if skip_pending:
            if stripped == "":
                continue  # blank lines between the comment and its element
            skip_pending = False
            if stripped.startswith("```"):
                skip_mode = "fence"
                continue
            if stripped.startswith("|"):
                skip_mode = "table"
                continue
            # Otherwise the skipped element is a heading/paragraph/etc., which
            # produces no items; fall through and process this line normally.

        # While waiting for / consuming a list-of-records table (outside code).
        if lor_name is not None and not in_python_block:
            if stripped.startswith("|"):
                if lor_start is None:
                    lor_start = line_num
                lor_end = line_num
                continue
            if stripped == "":
                # Blank before the table starts is fine; blank after ends it.
                if lor_start is not None:
                    flush_lor()
                continue
            # Any other content ends the table; fall through to process it.
            flush_lor()

        # Start of a Python code block
        if not in_python_block and stripped.startswith("```python"):
            in_python_block = True
            code_block_start = line_num
            code_lines = []
            continue

        # End of a code block
        if in_python_block and stripped == "```":
            in_python_block = False

            code_text = "".join(code_lines)
            if code_text.strip():
                try:
                    tree = ast.parse(code_text)
                except SyntaxError:
                    continue

                for node in ast.iter_child_nodes(tree):
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        # AST line numbers are 1-indexed within the code block.
                        # The first code line is code_block_start + 1, and ast
                        # lineno 1 maps to that line, so:
                        md_start = code_block_start + node.lineno
                        md_end = code_block_start + node.end_lineno
                        items.append((node.name, md_start, md_end, "code"))
            continue

        # Accumulate code-block lines
        if in_python_block:
            code_lines.append(line)
            continue

        # Track the current section (level-1/2 headings only; deeper headings
        # are subsections that don't change which section we're in).
        heading = HEADING_RE.match(stripped)
        if heading:
            if len(heading.group(1)) <= 2:
                section = heading.group(2).strip().lower()
            continue

        # A list-of-records comment; its table follows (after optional blanks).
        lor_match = LIST_OF_RECORDS_RE.search(stripped)
        if lor_match:
            lor_name = lor_match.group(1).upper()
            lor_start = None
            lor_end = None
            continue

        # Table rows with constant/type names (outside code blocks)
        match = TABLE_ROW_RE.match(stripped)
        if match:
            name = match.group(1)
            if CONSTANT_NAME_RE.match(name) or PASCAL_CASE_RE.match(name):
                kind = "table_def" if section in DEF_SECTIONS else "ref"
                items.append((name, line_num, line_num, kind))

    # Flush a list-of-records table that runs to EOF.
    flush_lor()

    return items


def build_source_map(repo_root):
    """
    Build the complete source map from a consensus-specs checkout.

    Returns ({"commit": <sha|None>, "items": {name: {fork: {file, start, end}}}},
    conflicts). Within a fork, a definition occurrence takes precedence over an
    incidental reference. Two distinct *table* definitions for the same item are
    reported as a conflict (likely a spurious reference table); code-block
    definitions may legitimately recur across files, so they resolve last-wins.
    """
    DEF_KINDS = {"code", "lor", "table_def"}
    source_map = {}
    kind_at = {}  # (name, fork) -> kind of the currently recorded location
    conflicts = []

    def record(md_file, fork_name):
        rel_path = os.path.relpath(md_file, repo_root)
        if rel_path in IGNORE_FILES:
            return
        for item_name, start_line, end_line, kind in extract_from_file(md_file):
            loc = {"file": rel_path, "start": start_line, "end": end_line}
            key = (item_name, fork_name)
            existing = source_map.setdefault(item_name, {}).get(fork_name)
            prev_kind = kind_at.get(key)

            new_def = kind in DEF_KINDS
            old_def = prev_kind in DEF_KINDS

            if existing is None:
                source_map[item_name][fork_name] = loc
                kind_at[key] = kind
            elif new_def and old_def:
                # Two table definitions disagreeing is a likely bug; code
                # definitions may recur legitimately, so just take the latest.
                if kind == "table_def" and prev_kind == "table_def" and existing != loc:
                    conflicts.append((item_name, fork_name, existing, loc))
                source_map[item_name][fork_name] = loc
                kind_at[key] = kind
            elif new_def and not old_def:
                # A real definition overrides a previously-seen reference.
                source_map[item_name][fork_name] = loc
                kind_at[key] = kind
            elif not new_def and old_def:
                # Ignore references once we have a definition.
                pass
            else:
                # Neither is a definition; keep the latest as a best-effort.
                source_map[item_name][fork_name] = loc
                kind_at[key] = kind

    for fork_name, fork_dir in find_fork_directories(repo_root):
        for md_file in find_markdown_files(fork_dir):
            record(md_file, fork_name)

    # Files that live outside specs/<fork>/ but contribute items to a fork.
    for rel_path, fork_name in EXTRA_FILES.items():
        md_file = os.path.join(repo_root, rel_path)
        if os.path.isfile(md_file):
            record(md_file, fork_name)

    return {"commit": get_commit(repo_root), "items": source_map}, conflicts


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: generate_links.py <consensus-specs-dir> [output-file]",
            file=sys.stderr,
        )
        sys.exit(1)

    repo_root = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.isdir(repo_root):
        print(f"Error: Directory not found: {repo_root}", file=sys.stderr)
        sys.exit(1)

    print(f"Extracting source map from: {repo_root}", file=sys.stderr)

    result, conflicts = build_source_map(repo_root)
    print(
        f"Extracted {len(result['items'])} items (commit {result['commit']})",
        file=sys.stderr,
    )

    if conflicts:
        print(f"ERROR: {len(conflicts)} duplicate definition(s):", file=sys.stderr)
        for name, fork, a, b in conflicts:
            print(
                f"  {name}#{fork}: {a['file']}#L{a['start']} vs {b['file']}#L{b['start']}",
                file=sys.stderr,
            )
        sys.exit(2)

    result_json = json.dumps(result)

    if output_file:
        out_dir = os.path.dirname(output_file)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result_json)
        print(f"Written to: {output_file}", file=sys.stderr)
    else:
        print(result_json)


if __name__ == "__main__":
    main()
