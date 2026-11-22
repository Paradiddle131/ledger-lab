"""The lock's dependency groups that run apart from the project's environment (`contract`, the Data
Contract CLI's DuckDB engine, and `bigquery`, dbt-bigquery for the metric versions gate's parse),
each in one environment per lock.

A group's environment is a folder in uv's cache (`uv cache dir`), `ledger-lab-groups/<group>-<key>`,
where the key is a hash of the group's name, `uv.lock` and `.python-version`; it holds that group
alone (`uv sync --frozen --only-group <group>`, which leaves the project and every other group out),
and a command runs that folder's own executables directly, in a fresh process each time. Nothing is
built per command, so a command that is killed leaves nothing behind; `uv run --isolated` built a
throwaway environment per command, which uv leaves in its cache when it is killed.

Several processes, pytest's workers among them, may ask at once: each takes a shared lock on the
folder's lock file (`<group>-<key>.lock`) and holds it until it exits; one that finds the folder not
built takes the lock exclusively, builds it, marks it built (`READY`) and goes back to the shared
lock, so the folder is built once and never changes while a process holds it. A changed lock or
Python version names a new folder; after taking its own, a process removes each other folder of the
group that no process holds (the lock files, empty, stay, so two processes never lock two files of
one name). Limits: `uv cache clean` and `uv cache prune` remove the folders with the rest of the
cache, so a command running at that moment fails and the next one builds the folder again; a
process asking while another holds a folder that was removed that way waits until the holder exits.

`python platform/group_env.py <group> <module>` (`make sync`) builds a group's environment, fetching
what uv's cache lacks, and imports `module` in it.
"""

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FOLDER = "ledger-lab-groups"
# The files that decide what a group's environment holds.
KEYED = ("uv.lock", ".python-version")
# The file a built environment holds, written once `uv sync` has finished.
READY = ".ledger-lab-ready"
# Each environment this process holds, with the open lock file that holds it until the process
# exits.
_HELD: dict[Path, int] = {}


class UnbuiltError(RuntimeError):
    """uv could not build a group's environment; `stderr` is what it wrote."""

    def __init__(self, message: str, stderr: str) -> None:
        super().__init__(message)
        self.stderr = stderr


def uv() -> str:
    return os.environ.get("UV") or shutil.which("uv") or "uv"


def cache_dir() -> Path:
    """uv's cache, as uv itself finds it (`UV_CACHE_DIR` and the rest)."""
    found = subprocess.run(  # noqa: S603
        [uv(), "cache", "dir"], capture_output=True, text=True, check=False
    )
    if found.returncode != 0 or not found.stdout.strip():
        raise UnbuiltError(
            f"uv could not name its cache (exit status {found.returncode})", found.stderr
        )
    return Path(found.stdout.strip())


def key(group: str, repo: Path = REPO) -> str:
    """The name of `group`'s environment for the lock and Python version of `repo`."""
    digest = hashlib.sha256(group.encode())
    for name in KEYED:
        path = repo / name
        digest.update(b"\0" + name.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"")
    return f"{group}-{digest.hexdigest()[:16]}"


def executable(folder: Path, name: str) -> Path:
    return folder / "bin" / name


def ready(folder: Path) -> bool:
    return (folder / READY).is_file() and executable(folder, "python").exists()


def build(folder: Path, group: str, repo: Path, offline: bool) -> None:
    """`group` alone synced into `folder`, from uv's cache only when `offline`."""
    (folder / READY).unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["UV_PROJECT_ENVIRONMENT"] = str(folder)
    command = [uv(), "sync", "--frozen", "--only-group", group, "--project", str(repo)]
    done = subprocess.run(  # noqa: S603
        [*command, *(["--offline"] if offline else [])],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
        env=env,
    )
    if done.returncode != 0 or not executable(folder, "python").exists():
        message = (
            f"uv could not build the {group} group's environment (exit status {done.returncode})"
        )
        raise UnbuiltError(message, done.stderr)
    (folder / READY).write_text("")


def remove_stale(base: Path, group: str, kept: str) -> None:
    """Each other environment of `group` under `base` that no process holds, removed."""
    for lock in base.glob(f"{group}-*.lock"):
        if lock.stem == kept:
            continue
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        try:
            folder = base / lock.stem
            (folder / READY).unlink(missing_ok=True)
            shutil.rmtree(folder, ignore_errors=True)
        finally:
            os.close(fd)


def environment(
    group: str, *, offline: bool = True, repo: Path = REPO, base: Path | None = None
) -> Path:
    """The folder of `group`'s environment for `repo`'s lock, under `base` (uv's cache's
    `FOLDER` by default), built when it is not; held by this process until it exits. UnbuiltError
    when uv cannot build it, from uv's cache alone when `offline`."""
    base = base or cache_dir() / FOLDER
    name = key(group, repo)
    folder = base / name
    if folder in _HELD:
        return folder
    base.mkdir(parents=True, exist_ok=True)
    fd = os.open(base / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        while True:
            fcntl.flock(fd, fcntl.LOCK_SH)
            if ready(folder):
                break
            fcntl.flock(fd, fcntl.LOCK_UN)
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not ready(folder):
                build(folder, group, repo, offline)
            fcntl.flock(fd, fcntl.LOCK_UN)
    except BaseException:
        os.close(fd)
        raise
    _HELD[folder] = fd
    remove_stale(base, group, name)
    return folder


def run_environment(folder: Path, env: dict[str, str]) -> dict[str, str]:
    """`env` as a command in `folder`'s environment gets it, as `uv run` gives it: the folder's
    executables first on `PATH`, and the folder as `VIRTUAL_ENV`."""
    path = env.get("PATH", "")
    bin_folder = str(executable(folder, ""))
    return env | {
        "PATH": os.pathsep.join(filter(None, [bin_folder, path])),
        "VIRTUAL_ENV": str(folder),
    }


def command(folder: Path, *argv: str) -> list[str]:
    """`argv` run in `folder`'s environment: its first word is that environment's executable of
    that name, never one found elsewhere on `PATH`."""
    first, *rest = argv
    return [str(executable(folder, first)), *rest]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: group_env.py <group> <module>", file=sys.stderr)
        return 2
    group, module = argv
    try:
        folder = environment(group, offline=False)
    except UnbuiltError as e:
        print(f"{e}\n{e.stderr}", file=sys.stderr)
        return 1
    imported = subprocess.run(  # noqa: S603
        command(folder, "python", "-c", f"import {module}"),
        check=False,
        env=run_environment(folder, dict(os.environ)),
    )
    return imported.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
