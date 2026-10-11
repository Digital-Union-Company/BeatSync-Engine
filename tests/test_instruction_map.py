"""The instruction map in `docs/claude/README.md` must stay complete.

The root `CLAUDE.md` nominates `docs/claude/README.md` as *the* map of the instruction
architecture, which makes the map a contract rather than a courtesy: a scoped rule that exists in
`.claude/rules/` but is absent from the map is invisible to anyone navigating by the documented
route. That is exactly what happened to `l2-stage-cache.md` (PR #36) and `render-worker.md`
(PR #37) — both shipped with valid `paths:` frontmatter and neither reached the table.

This suite is the mechanical guard for that class of drift. It reads the repository files directly
and imports **no** application module, so it runs on a bare CPython alongside the rest of the suite
(see `.claude/rules/test-harness.md` for the three coverage tiers).

Two deliberate design choices:

- **Structural parsing, not substring matching.** Asserting `"render-worker.md" in readme_text`
  would pass if the name merely appeared in prose, in the reference-document table, or in a
  sentence explaining why a rule was removed. The checks below parse the *table* under
  `## Subsystem rules` and look only at its first column.
- **Pure functions over text.** Every check is expressed against strings, so the negative controls
  can prove a defect is caught by perturbing an in-memory copy. No test here mutates a repository
  file.
"""

from __future__ import annotations

import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RULES_DIR = os.path.join(_REPO_ROOT, ".claude", "rules")
_README = os.path.join(_REPO_ROOT, "docs", "claude", "README.md")

#: The one rule that is deliberately unscoped — always active, so it belongs in the layer table at
#: the top of the map and must never be listed as a conditional subsystem rule.
UNSCOPED_RULE = "operating-policies.md"

_SUBSYSTEM_HEADING = "## Subsystem rules"

# A rule reference in the map is a backticked markdown filename, optionally path-qualified: the
# Subsystem rules table writes bare basenames (`pipeline-core.md`) while the layer table above it
# writes full paths (`.claude/rules/operating-policies.md`). Either way, capture the basename.
# Glob references such as `.claude/rules/*.md` deliberately do not match — `*` is not a basename.
_RULE_REF = re.compile(r"`(?:[A-Za-z0-9._/-]*/)?([A-Za-z0-9][A-Za-z0-9._-]*\.md)`")


class InstructionMapError(AssertionError):
    """A structural defect in the instruction map or in a rule's frontmatter."""


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------------------------
# Rule files
# --------------------------------------------------------------------------------------------


def rule_files() -> list[str]:
    """Every `.md` file in `.claude/rules/`, sorted."""
    return sorted(name for name in os.listdir(_RULES_DIR) if name.endswith(".md"))


def parse_frontmatter_paths(text: str, *, source: str) -> list[str] | None:
    """Return the `paths:` entries of a bounded YAML frontmatter block.

    Returns ``None`` when the file carries no frontmatter at all (an intentionally unscoped rule).
    Raises `InstructionMapError` when frontmatter is present but malformed: unterminated, missing
    `paths:`, or declaring an empty list. The distinction matters — "no frontmatter" is a valid,
    deliberate state, while "frontmatter that fails to declare paths" is a rule that silently never
    loads.

    Only the narrow YAML shape this repository actually uses is accepted: a `paths:` key followed by
    `- item` entries, quoted or bare.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise InstructionMapError(
            f"{source}: frontmatter opens with '---' but is never closed; "
            "the rule's paths: declaration cannot be bounded, so the rule will not load."
        )

    block = lines[1:end]
    paths: list[str] = []
    in_paths = False
    for raw in block:
        if not raw.strip():
            continue
        if re.fullmatch(r"paths:\s*", raw):
            in_paths = True
            continue
        item = re.fullmatch(r"\s+-\s+(.+?)\s*", raw)
        if in_paths and item:
            paths.append(item.group(1).strip().strip("\"'"))
            continue
        # Any other content at the top level ends the paths: list.
        if not raw.startswith((" ", "\t")):
            in_paths = False

    if not in_paths and not paths:
        raise InstructionMapError(
            f"{source}: frontmatter block present but it declares no 'paths:' key. "
            "A scoped rule without paths: never enters context; either add paths: or remove the "
            "frontmatter to make the rule deliberately unscoped."
        )
    if not paths:
        raise InstructionMapError(
            f"{source}: 'paths:' is declared but empty. An empty scope matches nothing, so the "
            "rule is dead weight; list the files it governs."
        )
    return paths


def scoped_rules() -> dict[str, list[str]]:
    """Map every scoped rule basename to its declared paths."""
    scoped = {}
    for name in rule_files():
        paths = parse_frontmatter_paths(_read(os.path.join(_RULES_DIR, name)), source=name)
        if paths is not None:
            scoped[name] = paths
    return scoped


# --------------------------------------------------------------------------------------------
# The map
# --------------------------------------------------------------------------------------------


def _table_rows(section: str) -> list[list[str]]:
    """Data rows of the first markdown table in `section`, as lists of stripped cells."""
    rows = []
    for line in section.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
            continue  # the |---|---| separator
        rows.append(cells)
    return rows[1:] if rows else rows  # drop the header row


def split_map(readme_text: str) -> tuple[str, str]:
    """Return (text before the subsystem heading, the subsystem section itself).

    The subsystem section runs from its heading to the next `## ` heading, so the reference-document
    table further down can never be mistaken for part of it.
    """
    start = readme_text.find(_SUBSYSTEM_HEADING)
    if start < 0:
        raise InstructionMapError(
            f"docs/claude/README.md: no '{_SUBSYSTEM_HEADING}' heading. The root CLAUDE.md points "
            "at this file as the map of the instruction architecture; the section is required."
        )
    rest = readme_text[start + len(_SUBSYSTEM_HEADING) :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    end = next_heading.start() if next_heading else len(rest)
    return readme_text[:start], rest[:end]


def mapped_rules(readme_text: str) -> list[str]:
    """Rule basenames in the first column of the Subsystem rules table, in document order.

    Duplicates are preserved so the duplicate guard can see them.
    """
    _, section = split_map(readme_text)
    rows = _table_rows(section)
    if not rows:
        raise InstructionMapError(
            "docs/claude/README.md: the Subsystem rules section contains no table rows."
        )
    names = []
    for cells in rows:
        match = _RULE_REF.search(cells[0])
        if match is None:
            raise InstructionMapError(
                f"docs/claude/README.md: Subsystem rules row {cells[0]!r} has no backticked "
                "`<rule>.md` reference in its first column."
            )
        names.append(match.group(1))
    return names


def layer_table_refs(readme_text: str) -> list[str]:
    """Rule paths referenced by the layer table above the Subsystem rules section."""
    preamble, _ = split_map(readme_text)
    refs = []
    for cells in _table_rows(preamble):
        for cell in cells:
            refs.extend(_RULE_REF.findall(cell))
    return refs


def check_map_complete(readme_text: str, scoped: dict[str, list[str]], existing: list[str]) -> None:
    """Raise `InstructionMapError` naming every defect in the map. The single aggregate check."""
    mapped = mapped_rules(readme_text)
    problems = []

    seen = set()
    duplicates = sorted({name for name in mapped if name in seen or seen.add(name)})
    if duplicates:
        problems.append(
            "duplicated in the Subsystem rules table: " + ", ".join(duplicates)
        )

    missing = sorted(set(scoped) - set(mapped))
    if missing:
        problems.append(
            "scoped rule(s) missing from the Subsystem rules table: "
            + ", ".join(missing)
            + " — add a row to docs/claude/README.md"
        )

    unknown = sorted(name for name in set(mapped) if name not in existing)
    if unknown:
        problems.append(
            "Subsystem rules table references nonexistent rule file(s): "
            + ", ".join(unknown)
            + " — no such file in .claude/rules/"
        )

    unscoped_listed = sorted(name for name in set(mapped) if name == UNSCOPED_RULE)
    if unscoped_listed:
        problems.append(
            f"{UNSCOPED_RULE} is listed as a conditional subsystem rule, but it is deliberately "
            "unscoped and always active; it belongs in the layer table only"
        )

    if problems:
        raise InstructionMapError("docs/claude/README.md: " + "; ".join(problems))


# --------------------------------------------------------------------------------------------
# A: every scoped rule is on the map
# --------------------------------------------------------------------------------------------


def test_every_scoped_rule_appears_in_the_subsystem_table():
    """The defect this suite exists for: a scoped rule that never reached the map."""
    check_map_complete(_read(_README), scoped_rules(), rule_files())


def test_the_two_previously_unmapped_rules_are_mapped():
    """Pinned by name, because these are the rules the map lost (PR #36 and PR #37)."""
    mapped = mapped_rules(_read(_README))
    assert "l2-stage-cache.md" in mapped
    assert "render-worker.md" in mapped


# --------------------------------------------------------------------------------------------
# B: the unscoped rule sits in the layer table, not among the subsystem rules
# --------------------------------------------------------------------------------------------


def test_operating_policies_is_in_the_layer_table_and_not_a_subsystem_rule():
    readme = _read(_README)
    assert any(ref == UNSCOPED_RULE for ref in layer_table_refs(readme)), (
        f"{UNSCOPED_RULE} must appear in the layer table at the top of docs/claude/README.md"
    )
    assert UNSCOPED_RULE not in mapped_rules(readme)


def test_operating_policies_is_genuinely_unscoped():
    """The layer table's claim is verified against the file, not taken on trust."""
    text = _read(os.path.join(_RULES_DIR, UNSCOPED_RULE))
    assert parse_frontmatter_paths(text, source=UNSCOPED_RULE) is None
    assert UNSCOPED_RULE not in scoped_rules()


# --------------------------------------------------------------------------------------------
# C: every reference resolves to a real file
# --------------------------------------------------------------------------------------------


def test_every_mapped_rule_basename_resolves_to_a_rule_file():
    existing = rule_files()
    for name in mapped_rules(_read(_README)):
        assert name in existing, f"{name} is on the map but absent from .claude/rules/"


def test_rule_set_is_exactly_the_scoped_rules_plus_the_unscoped_one():
    """No third category: a rule file is scoped, or it is `operating-policies.md`."""
    assert set(rule_files()) == set(scoped_rules()) | {UNSCOPED_RULE}


# --------------------------------------------------------------------------------------------
# D: frontmatter is bounded and declares paths
# --------------------------------------------------------------------------------------------


def test_every_scoped_rule_has_bounded_frontmatter_with_nonempty_paths():
    for name, paths in scoped_rules().items():
        text = _read(os.path.join(_RULES_DIR, name))
        assert text.startswith("---\n"), f"{name}: frontmatter must open on line 1"
        assert paths, f"{name}: paths: must not be empty"
        for entry in paths:
            assert entry, f"{name}: empty paths: entry"
            assert "\\" not in entry, f"{name}: {entry!r} uses a backslash; rules use '/'"


def test_frontmatter_parser_accepts_both_quoted_and_bare_path_entries():
    """`l2-stage-cache.md` writes bare entries; every other rule quotes them. Both are valid."""
    quoted = parse_frontmatter_paths(
        '---\npaths:\n  - "src/a.py"\n  - "tests/b.py"\n---\nbody\n', source="quoted"
    )
    bare = parse_frontmatter_paths(
        "---\npaths:\n  - src/a.py\n  - tests/b.py\n---\nbody\n", source="bare"
    )
    assert quoted == bare == ["src/a.py", "tests/b.py"]


# --------------------------------------------------------------------------------------------
# E: malformed paths declarations fail with a useful diagnostic
# --------------------------------------------------------------------------------------------


def test_unterminated_frontmatter_is_rejected():
    with pytest.raises(InstructionMapError) as caught:
        parse_frontmatter_paths('---\npaths:\n  - "src/a.py"\nbody\n', source="broken.md")
    assert "broken.md" in str(caught.value)
    assert "never closed" in str(caught.value)


def test_frontmatter_without_a_paths_key_is_rejected():
    with pytest.raises(InstructionMapError) as caught:
        parse_frontmatter_paths("---\ndescription: hello\n---\nbody\n", source="nopaths.md")
    assert "nopaths.md" in str(caught.value)
    assert "no 'paths:' key" in str(caught.value)


def test_empty_paths_list_is_rejected():
    with pytest.raises(InstructionMapError) as caught:
        parse_frontmatter_paths("---\npaths:\n---\nbody\n", source="empty.md")
    assert "empty.md" in str(caught.value)
    assert "empty" in str(caught.value)


def test_a_file_with_no_frontmatter_is_unscoped_rather_than_malformed():
    assert parse_frontmatter_paths("# Heading\n\nbody\n", source="plain.md") is None


# --------------------------------------------------------------------------------------------
# F: duplicate rows fail
# --------------------------------------------------------------------------------------------


def test_duplicate_subsystem_rows_are_rejected():
    readme = _read(_README)
    row = "| `render-worker.md` | `src/beatsync_fork/render_worker.py`"
    assert row in readme
    doubled = readme.replace(row, row + " |\n" + row, 1)
    with pytest.raises(InstructionMapError) as caught:
        check_map_complete(doubled, scoped_rules(), rule_files())
    assert "duplicated" in str(caught.value)
    assert "render-worker.md" in str(caught.value)


def test_the_live_map_has_no_duplicate_rows():
    mapped = mapped_rules(_read(_README))
    assert len(mapped) == len(set(mapped))


# --------------------------------------------------------------------------------------------
# G: negative control — dropping either recovered row must fail, by name
# --------------------------------------------------------------------------------------------


def _drop_row(readme_text: str, rule: str) -> str:
    """Remove `rule`'s row from the map, reproducing the defect this suite guards against."""
    kept = [line for line in readme_text.split("\n") if not line.startswith(f"| `{rule}`")]
    assert len(kept) == len(readme_text.split("\n")) - 1, f"expected exactly one row for {rule}"
    return "\n".join(kept)


@pytest.mark.parametrize("rule", ["l2-stage-cache.md", "render-worker.md"])
def test_removing_a_recovered_rule_row_fails_and_names_it(rule):
    """Proof the guard would have caught the defect at the pre-fix commit."""
    perturbed = _drop_row(_read(_README), rule)
    with pytest.raises(InstructionMapError) as caught:
        check_map_complete(perturbed, scoped_rules(), rule_files())
    message = str(caught.value)
    assert "missing from the Subsystem rules table" in message
    assert rule in message


def test_removing_both_recovered_rows_names_both():
    perturbed = _drop_row(_drop_row(_read(_README), "l2-stage-cache.md"), "render-worker.md")
    with pytest.raises(InstructionMapError) as caught:
        check_map_complete(perturbed, scoped_rules(), rule_files())
    message = str(caught.value)
    assert "l2-stage-cache.md" in message
    assert "render-worker.md" in message


# --------------------------------------------------------------------------------------------
# H: negative control — a fabricated rule reference must be rejected
# --------------------------------------------------------------------------------------------


def test_a_fabricated_rule_row_is_rejected_by_the_reverse_check():
    readme = _read(_README)
    fabricated = readme.replace(
        _SUBSYSTEM_HEADING + "\n\n| Rule | Scoped to (abridged) |\n|---|---|\n",
        _SUBSYSTEM_HEADING
        + "\n\n| Rule | Scoped to (abridged) |\n|---|---|\n"
        + "| `no-such-rule.md` | `src/nowhere.py` |\n",
        1,
    )
    assert fabricated != readme, "fabrication fixture failed to apply"
    with pytest.raises(InstructionMapError) as caught:
        check_map_complete(fabricated, scoped_rules(), rule_files())
    message = str(caught.value)
    assert "nonexistent rule file" in message
    assert "no-such-rule.md" in message


def test_listing_the_unscoped_rule_as_a_subsystem_rule_is_rejected():
    readme = _read(_README)
    perturbed = readme.replace(
        "| `test-harness.md` |",
        f"| `{UNSCOPED_RULE}` | `everything` |\n| `test-harness.md` |",
        1,
    )
    with pytest.raises(InstructionMapError) as caught:
        check_map_complete(perturbed, scoped_rules(), rule_files())
    assert "deliberately" in str(caught.value)
    assert UNSCOPED_RULE in str(caught.value)


# --------------------------------------------------------------------------------------------
# Parser self-checks — the structural claims the suite relies on
# --------------------------------------------------------------------------------------------


def test_substring_presence_alone_does_not_satisfy_the_map():
    """Guards the guard: a rule named only in prose must not count as mapped."""
    readme = _read(_README)
    prose_only = _drop_row(readme, "render-worker.md").replace(
        "## Reference documents",
        "`render-worker.md` is mentioned here in prose only.\n\n## Reference documents",
        1,
    )
    assert "render-worker.md" in prose_only  # a naive substring check would pass
    with pytest.raises(InstructionMapError) as caught:
        check_map_complete(prose_only, scoped_rules(), rule_files())
    assert "render-worker.md" in str(caught.value)


def test_reference_document_table_is_not_read_as_subsystem_rules():
    _, section = split_map(_read(_README))
    assert "instruction-architecture.md" not in section
    assert "stage5-cache-history.md" not in section


def test_map_row_count_equals_the_scoped_rule_count():
    assert len(mapped_rules(_read(_README))) == len(scoped_rules())
