"""
Setup script for update.py manual tests.

Creates a separate snapshot file for each test case in snapshots/tests/.
Run each test with:
    python3 update.py snapshots/tests/test_NN_<name>.txt

The original snapshot is never modified.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = REPO_ROOT / "snapshots"
TEST_DIR = REPO_ROOT / "snapshots" / "tests"

EN_START = 7      # 0-based line index of "## English (en)" in the snapshot
EN_END = 2357     # 0-based line index of the first non-English ## header


def find_latest_snapshot():
    """Return the most recent llms-full-*.txt snapshot file."""
    candidates = sorted(SNAPSHOTS_DIR.glob("llms-full-*.txt"))
    if not candidates:
        raise FileNotFoundError(f"No llms-full-*.txt snapshot found in {SNAPSHOTS_DIR}")
    return candidates[-1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load():
    snapshot = find_latest_snapshot()
    return snapshot.read_text(encoding="utf-8")

def write(name, text):
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    path = TEST_DIR / name
    path.write_text(text, encoding="utf-8")
    print(f"  Created: snapshots/tests/{name}")
    return path

def get_lines(text):
    return text.splitlines(keepends=True)

def find_section_links(lines, header):
    """Return (first_link_index, last_link_index) for a ### section in the English range."""
    in_section = False
    first = last = None
    for i in range(EN_START, EN_END):
        if lines[i].strip() == f"### {header}":
            in_section = True
            continue
        if in_section:
            if lines[i].startswith("### ") or lines[i].startswith("## "):
                break
            if lines[i].startswith("- ["):
                if first is None:
                    first = i
                last = i
    return first, last

def find_section_bounds(lines, header):
    """Return (header_idx, end_idx) — the full span of a ### section including blank lines."""
    header_idx = None
    for i in range(EN_START, EN_END):
        if lines[i].strip() == f"### {header}":
            header_idx = i
            break
    end_idx = header_idx + 1
    while end_idx < EN_END and not lines[end_idx].startswith("### ") and not lines[end_idx].startswith("## "):
        end_idx += 1
    return header_idx, end_idx


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def test_01_add_url():
    """Add a new link to an existing mapped section (Data API)."""
    lines = get_lines(load())
    new_link = "- [New Data API feature](https://help.claris.com/markdown/en/data-api-guide/new-feature.md)\n"
    first, _ = find_section_links(lines, "Claris FileMaker Data API Guide")
    lines.insert(first + 1, new_link)
    write("test_01_add_url.txt", "".join(lines))

def test_02_remove_url():
    """Remove a link from an existing mapped section (Data API)."""
    lines = get_lines(load())
    first, _ = find_section_links(lines, "Claris FileMaker Data API Guide")
    lines.pop(first)
    write("test_02_remove_url.txt", "".join(lines))

def test_03_change_display_name():
    """Change a link's display name, URL stays the same (Data API)."""
    lines = get_lines(load())
    first, _ = find_section_links(lines, "Claris FileMaker Data API Guide")
    url = re.search(r'\((https?://[^)]+)\)', lines[first]).group(1)
    lines[first] = f"- [RENAMED: Updated Display Name]({url})\n"
    write("test_03_change_display_name.txt", "".join(lines))

def test_04_change_url():
    """Change a link's URL, display name stays the same (Data API)."""
    lines = get_lines(load())
    first, _ = find_section_links(lines, "Claris FileMaker Data API Guide")
    label = re.search(r'\[([^\]]+)\]', lines[first]).group(1)
    lines[first] = f"- [{label}](https://help.claris.com/markdown/en/data-api-guide/changed-url.md)\n"
    write("test_04_change_url.txt", "".join(lines))

def test_05_empty_section():
    """Section header exists but has no link lines (Data API)."""
    lines = get_lines(load())
    first, last = find_section_links(lines, "Claris FileMaker Data API Guide")
    del lines[first:last + 1]
    write("test_05_empty_section.txt", "".join(lines))

def test_06_remove_entire_section():
    """Remove an entire section header and all its links (OData)."""
    lines = get_lines(load())
    header_idx, end_idx = find_section_bounds(lines, "Claris FileMaker OData Guide")
    del lines[header_idx:end_idx]
    write("test_06_remove_entire_section.txt", "".join(lines))

def test_07_new_section_in_map():
    """Remove Admin API section then add it back with two fake links."""
    lines = get_lines(load())
    header_idx, end_idx = find_section_bounds(lines, "Claris FileMaker Admin API Guide")
    del lines[header_idx:end_idx]
    new_block = (
        "### Claris FileMaker Admin API Guide\n"
        "\n"
        "- [New Admin API call](https://help.claris.com/markdown/en/admin-api-guide/new-call.md)\n"
        "- [Another new Admin API call](https://help.claris.com/markdown/en/admin-api-guide/another-call.md)\n"
        "\n"
    )
    for j, line in enumerate(new_block.splitlines(keepends=True)):
        lines.insert(header_idx + j, line)
    write("test_07_new_section_in_map.txt", "".join(lines))

def test_08_new_section_not_in_map():
    """Add a brand new section with no SECTION_MAP entry."""
    lines = get_lines(load())
    first_section = next(i for i in range(EN_START, EN_END) if lines[i].startswith("### "))
    new_block = (
        "### Claris Some New Product Guide\n"
        "\n"
        "- [Getting started](https://help.claris.com/markdown/en/new-product/getting-started.md)\n"
        "- [Reference](https://help.claris.com/markdown/en/new-product/reference.md)\n"
        "\n"
    )
    for j, line in enumerate(new_block.splitlines(keepends=True)):
        lines.insert(first_section + j, line)
    write("test_08_new_section_not_in_map.txt", "".join(lines))

def test_09_split_known_slug():
    """Add a link with a known Get function slug to the Pro Help SPLIT section."""
    lines = get_lines(load())
    first, _ = find_section_links(lines, "Claris FileMaker Pro Help")
    new_link = "- [Get(NewFunction)](https://help.claris.com/markdown/en/pro-help/get-new-function.md)\n"
    lines.insert(first, new_link)
    write("test_09_split_known_slug.txt", "".join(lines))

def test_10_split_unknown_slug():
    """Add a link with an unknown slug to the Pro Help SPLIT section (should hit catch-all)."""
    lines = get_lines(load())
    first, _ = find_section_links(lines, "Claris FileMaker Pro Help")
    new_link = "- [Brand new page](https://help.claris.com/markdown/en/pro-help/brand-new-page.md)\n"
    lines.insert(first, new_link)
    write("test_10_split_unknown_slug.txt", "".join(lines))

def test_11_split_remove_all_links():
    """Remove all links from the Pro Help SPLIT section."""
    lines = get_lines(load())
    first, last = find_section_links(lines, "Claris FileMaker Pro Help")
    del lines[first:last + 1]
    write("test_11_split_remove_all_links.txt", "".join(lines))

def test_12_missing_references_dir():
    """Instructions only — no snapshot modification needed. See test plan."""
    print("  Test 12: Delete a skill's references/ directory manually, then run:")
    print("    python3 update.py")
    print("  Expected: directory recreated, all files written correctly.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    snapshot = find_latest_snapshot()
    print(f"Source snapshot: {snapshot.name}")
    print(f"Writing test files to: snapshots/tests/\n")

    test_01_add_url()
    test_02_remove_url()
    test_03_change_display_name()
    test_04_change_url()
    test_05_empty_section()
    test_06_remove_entire_section()
    test_07_new_section_in_map()
    test_08_new_section_not_in_map()
    test_09_split_known_slug()
    test_10_split_unknown_slug()
    test_11_split_remove_all_links()
    test_12_missing_references_dir()

    print(f"\nDone. Run each test with:")
    print(f"  python3 update.py snapshots/tests/test_NN_<name>.txt")
