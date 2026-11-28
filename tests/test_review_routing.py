"""The lenses `REVIEW.md` gives a few named paths, read from the working tree's `REVIEW.md`.

The review skill's `tier.sh` reads `REVIEW.md` from the default branch, so a change's own edit
of it is judged here instead: this file parses it by the rules `tier.sh` states (`parse` and
`glob_re` below, the one copy of them in this repository) and pins the lenses of the paths in
PINNED (`ingest/individuals.py`, `ingest/chicago/table.py`, the payments mart, a Chicago sample,
`access/personas.yaml`, `access/thresholds.py`, `models/models/marts/payments/payments_masked.sql`,
`platform/gcp/warehouse.tf`, `platform/gcp/budget.tf`, `platform/fences.py`, `ingest/load.py`,
`ingest/bucket.py`), several from each of its three areas.

Checked:
- `REVIEW.md` parses: `parse` raises where `tier.sh` would warn and count every path as
  sensitive, and the test fails with the reason;
- no `## Sensitive` glob matches a path in LEFT_OUT;
- each path in PINNED gets exactly its pinned lenses: the union of the `; lenses:` of every
  `## Sensitive` item with a glob that matches it, as `tier.sh` computes it; so a pinned path's
  glob changed to match nothing, its item's lenses clause dropped, or a lens removed from it,
  turns it red when no other item matching that path gives the same lenses;
- `parse` refuses each text in REFUSED: a second `## Sensitive` or `## Routine`; a non-blank
  line under either that is not a `- ` or `* ` item; `; lenses:` on a `## Routine` item; a
  lenses clause that does not end the line; a backtick or `;` in the reason after ` — `; an
  item with no backticked glob, or with other text beside its globs; and a glob with a leading
  or trailing space, a leading `/` or a `\\`;
- `parse` and `lenses_for` read the examples in the reading test as the stated rules say: only
  a level-two `## Sensitive` heading opens the list, `- ` and `* ` items, a reason and a lenses
  clause, an item with no lenses; a glob without a slash matches the file name at the root and
  below, `**` spans folders, `*` and `?` stay within one, and a glob matches the whole path;
- where the review skill's `tier.sh` is found (`$LOADOUT/skills/review/scripts/tier.sh`, with
  a relative `LOADOUT` read from the repository root; `make test` passes the Makefile's
  `LOADOUT`), run on a scratch repository whose base holds the text as its `REVIEW.md`: for the
  working tree's `REVIEW.md` it prints `sensitive`, each pinned path with its pinned lenses, and
  no warning, also when `LOADOUT` is given relative; for each REFUSED text it warns that it
  cannot parse it. With no `LOADOUT`, or no `tier.sh` under it, these cross-checks are reported
  as skipped, with the reason.

Not checked:
- paths not in PINNED: their globs and lenses may change with this test green;
- a lens dropped from one item while another item matching the same pinned path still gives it
  (the union, which is what the review gets, is unchanged);
- `tier.sh`'s floor, its `@path` imports and its `## Routine` rules;
- that each glob in `REVIEW.md` matches a tracked file, or that `## What each item guards`
  follows the items;
- agreement of `parse` with `tier.sh` beyond the working tree's `REVIEW.md` on the pinned paths
  and the REFUSED texts, and agreement at all when the cross-checks skip.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REVIEW = REPO / "REVIEW.md"
GIT = shutil.which("git") or "git"
BASH = shutil.which("bash") or "bash"

PINNED = {
    "ingest/individuals.py": {"control-prober", "leak-hunter"},
    "access/personas.yaml": {"control-prober", "guardrail-reviewer", "leak-hunter"},
    "access/thresholds.py": {"control-prober", "guardrail-reviewer", "leak-hunter"},
    "models/models/marts/payments/payments_masked.sql": {
        "control-prober",
        "guardrail-reviewer",
        "leak-hunter",
    },
    "platform/gcp/warehouse.tf": {"control-prober", "guardrail-reviewer", "leak-hunter"},
    "platform/gcp/budget.tf": {"control-prober", "guardrail-reviewer"},
    "platform/fences.py": {"control-prober", "guardrail-reviewer"},
    "ingest/chicago/table.py": {"control-prober", "leak-hunter"},
    "models/models/marts/payments/payments.sql": {"control-prober", "leak-hunter"},
    "models/seeds/staging/chicago/chicago_payments_sample.csv": {"control-prober", "leak-hunter"},
    "ingest/load.py": {"control-prober", "guardrail-reviewer"},
    "ingest/bucket.py": {"control-prober", "guardrail-reviewer"},
}

# Paths outside the three areas, which no `## Sensitive` item matches.
LEFT_OUT = (
    "ingest/ecb/rates.py",
    "platform/cost_check.py",
    "agent/policy/tools.py",
    "tests/conftest.py",
)


# Short REVIEW.md texts, each one that tier.sh's parser refuses.
REFUSED = [
    "## Sensitive\n- `a`\n## Sensitive\n- `b`\n",
    "## Routine\n- `a`\n## Routine\n- `b`\n",
    "## Sensitive\n`ingest/**`; lenses: leak-hunter\n",
    "## Routine\n- `docs/**`; lenses: leak-hunter\n",
    "## Sensitive\n- `a` — a reason; with a semicolon\n",
    "## Sensitive\n- `a` — a reason naming `b`\n",
    "## Sensitive\n- a, b; lenses: leak-hunter\n",
    "## Sensitive\n- `a` and prose; lenses: leak-hunter\n",
    "## Sensitive\n- ` a`\n",
    "## Sensitive\n- `/a`\n",
    "## Sensitive\n- `a\\b`\n",
    "## Sensitive\n- `a `\n",
    "## Routine\nprose\n",
    "## Sensitive\n- `x.py`; lenses: leak-hunter — why\n",
    "## Sensitive\n- ; lenses: leak-hunter\n",
]


class Unparsable(Exception):
    pass


LENSES = re.compile(r";\s*lenses:\s*([\w-]+(?:\s*,\s*[\w-]+)*)\s*$")


def parse(text: str) -> list[tuple[list[str], set[str]]]:
    """The `## Sensitive` items as (globs, lenses); raises Unparsable where `tier.sh` would."""
    items: list[tuple[list[str], set[str]]] = []
    section: str | None = None
    seen: set[str] = set()
    for n, line in enumerate(text.splitlines(), 1):
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if heading:
            name = heading.group(2).strip().lower()
            if len(heading.group(1)) == 2 and name in ("sensitive", "routine"):
                if name in seen:
                    raise Unparsable(f"line {n}: a second `## {heading.group(2)}`")
                seen.add(name)
                section = name
            else:
                section = None
            continue
        if section is None or not line.strip():
            continue
        item = re.match(r"^\s*[-*]\s+(.*)$", line)
        if not item:
            raise Unparsable(f"line {n}: under `## {section.title()}` every line is a `- ` item")
        rest, lenses = item.group(1), ""
        clause = LENSES.search(rest)
        if clause:
            if section != "sensitive":
                raise Unparsable(f"line {n}: lenses belong to sensitive globs")
            rest, lenses = rest[: clause.start()], clause.group(1)
        rest, dash, reason = rest.partition(" — ")
        if dash and re.search(r"[`;]", reason):
            raise Unparsable(f"line {n}: a reason after ` — ` holds a backtick or a `;`")
        globs = re.findall(r"`([^`]+)`", rest)
        if not globs or re.sub(r"`[^`]+`", "", rest).strip(" ,"):
            raise Unparsable(f"line {n}: an item is backticked globs, then its lenses")
        for glob in globs:
            if glob.strip() != glob or glob.startswith("/") or "\\" in glob:
                raise Unparsable(f"line {n}: `{glob}` is not a glob tier.sh reads")
        if section == "sensitive":
            items.append((globs, {x.strip() for x in lenses.split(",") if x.strip()}))
    return items


def glob_re(glob: str) -> re.Pattern[str]:
    if "/" not in glob.rstrip("/"):
        glob = "**/" + glob
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out + "$")


def lenses_for(path: str, items: list[tuple[list[str], set[str]]]) -> set[str]:
    return {
        lens
        for globs, lenses in items
        if any(glob_re(g).match(path) for g in globs)
        for lens in lenses
    }


def review_items() -> list[tuple[list[str], set[str]]]:
    try:
        return parse(REVIEW.read_text())
    except Unparsable as exc:
        pytest.fail(f"tier.sh would refuse REVIEW.md and route every path as sensitive: {exc}")


def test_review_md_parses_by_the_tier_scripts_rules() -> None:
    assert review_items()


@pytest.mark.parametrize("path", sorted(PINNED))
def test_review_md_gives_a_pinned_path_its_lenses(path: str) -> None:
    assert sorted(lenses_for(path, review_items())) == sorted(PINNED[path])


@pytest.mark.parametrize("path", LEFT_OUT)
def test_review_md_leaves_a_path_outside_its_areas_off_the_sensitive_list(path: str) -> None:
    assert not any(glob_re(g).match(path) for globs, _ in review_items() for g in globs)


@pytest.mark.parametrize("text", REFUSED)
def test_the_parser_refuses_what_tier_sh_refuses(text: str) -> None:
    with pytest.raises(Unparsable):
        parse(text)


def test_the_parser_reads_items_globs_and_lenses_by_the_stated_rules() -> None:
    items = parse(
        "# Review\nprose\n# Sensitive\n- `top.py`; lenses: leak-hunter\n"
        "### Sensitive\n- `deeper.py`; lenses: leak-hunter\n"
        "## Sensitive\n"
        "- `x.py`, `src/**`; lenses: leak-hunter, control-prober\n"
        "- `one/*.py` — a reason; lenses: claims-auditor\n"
        "- `two/a?c.py`; lenses: guardrail-reviewer\n"
        "- `three/**`\n"
        "* `star.py`; lenses: claims-auditor\n"
        "## Other\nprose\n"
        "## Routine\n- `docs/**` — prose\n"
    )
    assert items == [
        (["x.py", "src/**"], {"leak-hunter", "control-prober"}),
        (["one/*.py"], {"claims-auditor"}),
        (["two/a?c.py"], {"guardrail-reviewer"}),
        (["three/**"], set()),
        (["star.py"], {"claims-auditor"}),
    ]
    assert lenses_for("x.py", items) == {"leak-hunter", "control-prober"}
    assert lenses_for("deep/down/x.py", items) == {"leak-hunter", "control-prober"}
    assert lenses_for("x.pyc", items) == set()
    assert lenses_for("deep/x.py.bak", items) == set()
    assert lenses_for("top.py", items) == set()
    assert lenses_for("deeper.py", items) == set()
    assert lenses_for("a/star.py", items) == {"claims-auditor"}
    assert lenses_for("src/a/b/c.txt", items) == {"leak-hunter", "control-prober"}
    assert lenses_for("one/a.py", items) == {"claims-auditor"}
    assert lenses_for("one/a/b.py", items) == set()
    assert lenses_for("other/one/a.py", items) == set()
    assert lenses_for("two/abc.py", items) == {"guardrail-reviewer"}
    assert lenses_for("two/a/c.py", items) == set()
    assert lenses_for("two/ac.py", items) == set()
    assert lenses_for("three/y.py", items) == set()  # matched, with no lenses


def tier_sh(tmp_path: Path, review: str, paths: list[str]) -> subprocess.CompletedProcess[str]:
    """Runs tier.sh on a scratch repository: a base holding `review` as REVIEW.md, then `paths`."""
    loadout = os.environ.get("LOADOUT", "")
    tier = (REPO / loadout / "skills" / "review" / "scripts" / "tier.sh").resolve()
    if not loadout or not tier.is_file():
        pytest.skip("no review skill's tier.sh under $LOADOUT: the cross-check with it did not run")

    def git(*args: str) -> None:
        subprocess.run([GIT, "-C", str(tmp_path), *args], capture_output=True, check=True)

    identity = (
        "-c",
        "user.name=review-routing-test",
        "-c",
        "user.email=",
        "-c",
        "commit.gpgsign=false",
    )
    git("init", "--quiet", "--initial-branch=base")
    (tmp_path / "REVIEW.md").write_text(review)
    git("add", "REVIEW.md")
    git(*identity, "commit", "--quiet", "--no-verify", "-m", "base")
    git("checkout", "--quiet", "-b", "change")
    for path in paths:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("changed\n")
    git("add", "--", *paths)
    git(*identity, "commit", "--quiet", "--no-verify", "-m", "change")
    return subprocess.run(
        [BASH, str(tier), "--base", "base", "--head", "change"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )


def test_tier_sh_prints_the_pinned_lenses_for_the_working_trees_review_md(
    tmp_path: Path,
) -> None:
    assert_prints_the_pinned_lenses(tier_sh(tmp_path, REVIEW.read_text(), sorted(PINNED)))


def assert_prints_the_pinned_lenses(done: subprocess.CompletedProcess[str]) -> None:
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    lines = done.stdout.splitlines()
    assert lines[0] == "sensitive", done.stdout
    printed = {}
    for line in lines[1:]:
        found = re.match(r"^(\S+)(?: \(lenses: (.*)\))?$", line)
        assert found, line
        printed[found.group(1)] = set((found.group(2) or "").split(", ")) - {""}
    assert printed == PINNED


@pytest.mark.parametrize("text", REFUSED)
def test_tier_sh_refuses_each_text_the_parser_refuses(tmp_path: Path, text: str) -> None:
    done = tier_sh(tmp_path, text, ["src/changed.py"])
    assert done.returncode == 0, done.stderr
    assert "cannot be parsed" in done.stderr, done.stderr


def test_tier_sh_cross_check_reads_a_loadout_relative_to_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loadout = os.environ.get("LOADOUT", "")
    if not loadout or not (REPO / loadout / "skills" / "review" / "scripts" / "tier.sh").is_file():
        pytest.skip("no review skill's tier.sh under $LOADOUT: the cross-check with it did not run")
    # The Makefile's fallback, `../loadout`, is relative to the repository root.
    monkeypatch.setenv("LOADOUT", os.path.relpath((REPO / loadout).resolve(), REPO))
    assert not Path(os.environ["LOADOUT"]).is_absolute()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    try:
        done = tier_sh(scratch, REVIEW.read_text(), sorted(PINNED))
    except pytest.skip.Exception as skipped:
        pytest.fail(f"the cross-check skipped with a relative LOADOUT: {skipped}")
    assert_prints_the_pinned_lenses(done)
