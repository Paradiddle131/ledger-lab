"""The lenses `REVIEW.md` gives a few named paths, read from the working tree's `REVIEW.md`.

The review skill's `tier.sh` reads `REVIEW.md` from the default branch, so a change's own edit
of it is judged here instead: this file parses it by the rules `tier.sh` states (`parse` and
`glob_re` below, the one copy of them in this repository) and pins the lenses of the paths in
PINNED (`ingest/load.py`, `ingest/bucket.py`, `access/personas.yaml`, `platform/cost_check.py`).

Checked:
- `REVIEW.md` parses with none of the refusals `tier.sh` makes (each makes `tier.sh` warn and
  count every path as sensitive): a second `## Sensitive` or `## Routine`, a non-blank line
  under either that is not a `- ` or `* ` item, `; lenses:` on a `## Routine` item, a backtick
  or `;` in the reason after ` — `, an item that is not backticked globs (its backticks removed,
  say), and a glob with a leading or trailing space, a leading `/` or a `\\`;
- each path in PINNED gets exactly its pinned lenses: the union of the `; lenses:` of every
  `## Sensitive` item with a glob that matches it, as `tier.sh` computes it; so a pinned path's
  glob changed to match nothing, its item's lenses clause dropped, or a lens removed from it,
  turns it red when no other item matching that path gives the same lenses;
- `parse` refuses each of those refusals on a one-line example (REFUSED), and reads a glob
  without a slash at any depth, `**` across folders, and `*` and `?` within one folder;
- where the review skill's `tier.sh` is found (`$LOADOUT/skills/review/scripts/tier.sh`;
  `make test` passes the Makefile's `LOADOUT`), run on a scratch repository whose base holds
  the text as its `REVIEW.md`: for the working tree's `REVIEW.md` it prints `sensitive`, each
  pinned path with its pinned lenses, and no warning; for each REFUSED text it warns that it
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
    "ingest/load.py": {"control-prober", "guardrail-reviewer", "leak-hunter"},
    "ingest/bucket.py": {"control-prober", "guardrail-reviewer", "leak-hunter"},
    "access/personas.yaml": {
        "claims-auditor",
        "control-prober",
        "guardrail-reviewer",
        "leak-hunter",
    },
    "platform/cost_check.py": {"control-prober", "guardrail-reviewer"},
}


# One-line REVIEW.md texts, one per refusal of tier.sh's parser.
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


@pytest.mark.parametrize("text", REFUSED)
def test_the_parser_refuses_what_tier_sh_refuses(text: str) -> None:
    with pytest.raises(Unparsable):
        parse(text)


def test_the_parser_reads_items_globs_and_lenses_as_tier_sh_does() -> None:
    items = parse(
        "# Review\nprose\n## Sensitive\n"
        "- `x.py`, `src/**`; lenses: leak-hunter, control-prober\n"
        "- `one/*.py` — a reason; lenses: claims-auditor\n"
        "- `two/a?c.py`; lenses: guardrail-reviewer\n"
        "- `three/**`\n"
        "## Other\nprose\n"
        "## Routine\n- `docs/**` — prose\n"
    )
    assert items == [
        (["x.py", "src/**"], {"leak-hunter", "control-prober"}),
        (["one/*.py"], {"claims-auditor"}),
        (["two/a?c.py"], {"guardrail-reviewer"}),
        (["three/**"], set()),
    ]
    assert lenses_for("deep/down/x.py", items) == {"leak-hunter", "control-prober"}
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
    tier = Path(loadout) / "skills" / "review" / "scripts" / "tier.sh"
    if not loadout or not tier.is_file():
        pytest.skip("no review skill's tier.sh under $LOADOUT: the cross-check with it did not run")

    def git(*args: str) -> None:
        subprocess.run([GIT, "-C", str(tmp_path), *args], capture_output=True, check=True)

    identity = ("-c", "user.name=review-routing-test", "-c", "user.email=")
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
    done = tier_sh(tmp_path, REVIEW.read_text(), sorted(PINNED))
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
