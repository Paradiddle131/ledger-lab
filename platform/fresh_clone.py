"""`make fresh-clone`: clone the committed `HEAD` and prove it holds no secret, identifier or data.

A clone sees only what is committed, so this checks what a stranger would get. It fails on:
- a tracked file with a data extension (`DATA_EXTENSIONS`, matched in any letter case) other than
  the dbt seeds listed in `SEEDS`, whatever `.gitignore` says, so narrowing or deleting an ignore
  rule lets no data in;
- a tracked file that `.gitignore` excludes (data, credentials and tool state can only get in by
  `git add -f`);
- a `.gitignore` exception (a `!` line) other than one naming a listed seed in the root file;
- an email address, other than on the reserved example domains;
- a GCP project id written as a value (a `project` setting, a `projects/<id>` path, a service
  account's domain), other than the documented placeholders;
- a service-account key or any private key;
- anything the secret scan (detect-secrets, pinned in the lock) reports.
"""

import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
EXAMPLE_DOMAINS = re.compile(r"(?:^|\.)example\.(?:com|org|net)$|\.(?:example|invalid|test)$")

_ID = r"([a-z][a-z0-9-]{4,28}[a-z0-9])"
PROJECT_VALUES = [
    re.compile(r"""\bprojects/""" + _ID + r"\b"),
    re.compile(r"""--project(?:_id)?[= ]['"]?""" + _ID + r"\b"),
    re.compile(r"""\b(?:GCP_PROJECT|LEDGER_LAB_GCP_PROJECT|GOOGLE_CLOUD_PROJECT)=['"]?""" + _ID),
    re.compile(r"""\bproject(?:_id)?['"]?\s*[:=]\s*['"]""" + _ID + r"""['"]"""),
    re.compile(r"""@""" + _ID + r"""\.iam\.gserviceaccount\.com"""),
]
# Values the docs and tests use in place of a real project.
PLACEHOLDER_PROJECTS = {"the-lab", "another-lab", "example-project", "my-project"}

KEY_MARKERS = [
    re.compile(r""""type"\s*:\s*"service_account\""""),
    re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
]

# The extensions `.gitignore` names as data; a tracked file ending in one is refused.
DATA_EXTENSIONS = (
    ".csv",
    ".tsv",
    ".xlsx",
    ".xls",
    ".parquet",
    ".zip",
    ".gz",
    ".db",
    ".sqlite",
    ".duckdb",
)
# The only data files the tree holds: dbt seeds, reference tables authored here or a licence-allowed
# sample of real data the tests need.
# Each is named exactly, here and in `.gitignore`, so a new seed is a reviewed change to both.
SEEDS = {
    "models/seeds/calendars/fiscal_year_starts.csv",
    "models/seeds/calendars/period_status.csv",
    "models/seeds/reference/fx/ecb_rates_sample.csv",
    "models/seeds/staging/nl_government/nl_government_budget_sample.csv",
}
ALLOWED_EXCEPTIONS = {f"!/{seed}" for seed in SEEDS}

DETECT_SECRETS = Path(sys.executable).with_name("detect-secrets-hook")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def tracked(repo: Path) -> list[str]:
    return [p for p in git(repo, "ls-files", "-z").split("\0") if p]


def ignored_but_tracked(repo: Path) -> list[str]:
    out = git(repo, "ls-files", "-z", "--cached", "--ignored", "--exclude-standard")
    return [p for p in out.split("\0") if p]


def data_files(files: list[str]) -> Iterable[str]:
    for name in files:
        if name.lower().endswith(DATA_EXTENSIONS) and name not in SEEDS:
            yield f"{name}: data file outside the listed seeds"


def widened_ignores(repo: Path, files: list[str]) -> Iterable[str]:
    """Every `!` line of a tracked `.gitignore` but a listed seed's in the root one."""
    for name in files:
        if Path(name).name != ".gitignore":
            continue
        allowed = ALLOWED_EXCEPTIONS if name == ".gitignore" else set()
        for number, line in enumerate((repo / name).read_text().splitlines(), 1):
            if line.startswith("!") and line.strip() not in allowed:
                yield f"{name}:{number}: exception {line.strip()!r} beyond the listed seeds"


def text_of(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def line_problems(name: str, text: str) -> Iterable[str]:
    for number, line in enumerate(text.splitlines(), 1):
        where = f"{name}:{number}"
        for match in EMAIL.finditer(line):
            if not EXAMPLE_DOMAINS.search(match.group(1).lower()):
                yield f"{where}: email address"
        for pattern in PROJECT_VALUES:
            for match in pattern.finditer(line):
                if match.group(1) not in PLACEHOLDER_PROJECTS:
                    yield f"{where}: GCP project id {match.group(1)!r}"
        if any(marker.search(line) for marker in KEY_MARKERS):
            yield f"{where}: key material"


def secret_scan(repo: Path, files: list[str]) -> list[str]:
    if not files:
        return []
    result = subprocess.run(  # noqa: S603
        [str(DETECT_SECRETS), *files], capture_output=True, text=True, check=False, cwd=repo
    )
    if result.returncode == 0:
        return []
    return [f"secret scan: {line}" for line in result.stdout.splitlines() if line.strip()]


def problems(repo: Path) -> list[str]:
    """Everything in the repo's tracked files that a public clone must not hold."""
    files = tracked(repo)
    found = [
        f"{name}: tracked although .gitignore excludes it" for name in ignored_but_tracked(repo)
    ]
    found.extend(data_files(files))
    found.extend(widened_ignores(repo, files))
    for name in files:
        text = text_of(repo / name)
        if text is not None:
            found.extend(line_problems(name, text))
    found.extend(secret_scan(repo, files))
    return found


def check_clone(source: Path) -> list[str]:
    """Clone the source's committed HEAD into a temporary folder and check the clone."""
    with tempfile.TemporaryDirectory(prefix="fresh-clone-") as tmp:
        clone = Path(tmp) / "clone"
        subprocess.run(  # noqa: S603
            ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(clone)],  # noqa: S607
            capture_output=True,
            check=True,
        )
        return problems(clone)


def main() -> int:
    source = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    found = check_clone(source)
    for line in found:
        print(line)
    count = len(tracked(source))
    print(
        f"fresh-clone: {count} files, {len(found)} problems"
        if found
        else f"fresh-clone: {count} files, no secret, identifier or data"
    )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
