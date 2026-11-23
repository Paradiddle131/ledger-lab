"""The lock groups' environments (platform/group_env.py): one per group and lock, built once however
many processes ask at once, holding the group alone, and removed once stale only while no process
holds it. Each case builds the `contract` group from uv's cache with no network, in a folder of its
own under the test's temporary folder, so the environments the gate and the tests use are not
touched."""

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import group_env
import pytest

PLATFORM = Path(group_env.__file__).resolve().parent
GROUP = "contract"
# A process that asks for the group's environment under the folder it is given, writing a line to
# the log for each build it makes, and printing the folder it got.
ASK = """
import sys
from pathlib import Path

import group_env

build = group_env.build


def logged(*args, **kwargs):
    with open(sys.argv[2], "a") as log:
        log.write("built\\n")
    build(*args, **kwargs)


group_env.build = logged
print(group_env.environment(sys.argv[3], base=Path(sys.argv[1])))
"""


@pytest.fixture(scope="module")
def cached() -> None:
    """The `contract` group's environment can be built offline, as `make sync` leaves it."""
    try:
        group_env.environment(GROUP)
    except group_env.UnbuiltError as e:
        pytest.skip(f"{e}: run `make sync`")


def ask(base: Path, log: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", ASK, str(base), str(log), GROUP],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | {"PYTHONPATH": str(PLATFORM)},
    )


def test_processes_asking_at_once_build_the_environment_once(tmp_path: Path, cached: None) -> None:
    """Four processes that ask at once, as pytest's workers do, get one folder, built once."""
    base, log = tmp_path / "groups", tmp_path / "builds.log"
    asking = [ask(base, log) for _ in range(4)]
    answers = [p.communicate(timeout=300) for p in asking]
    assert [p.returncode for p in asking] == [0] * 4, [err for _, err in answers]
    folders = {Path(out.strip()) for out, _ in answers}
    assert folders == {base / group_env.key(GROUP)}
    assert log.read_text() == "built\n"
    assert group_env.ready(base / group_env.key(GROUP))
    assert ask(base, log).communicate(timeout=300)[0].strip() == str(*folders)
    assert log.read_text() == "built\n"


def test_the_environment_holds_the_group_alone(cached: None) -> None:
    """The group's Python reads its own folder's packages, never the project's: the CLI's DuckDB
    engine is there, and `dev`'s tools are not."""
    folder = group_env.environment(GROUP)
    probe = (
        "import importlib.util as u, sys;"
        " print(sys.prefix, u.find_spec('datacontract') is not None, u.find_spec('pytest') is None)"
    )
    done = subprocess.run(  # noqa: S603
        group_env.command(folder, "python", "-c", probe),
        capture_output=True,
        text=True,
        check=True,
        env=group_env.run_environment(folder, dict(os.environ)),
    )
    assert done.stdout.split() == [str(folder), "True", "True"]


def stale(base: Path, name: str) -> Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / group_env.READY).write_text("")
    (base / f"{name}.lock").touch()
    return folder


def test_a_stale_environment_is_removed_only_while_no_process_holds_it(
    tmp_path: Path, cached: None
) -> None:
    """Another lock's environment of the group goes once a process takes its own, unless a process
    holds it; another group's stays."""
    base = tmp_path / "groups"
    unheld = stale(base, f"{GROUP}-0000000000000000")
    held = stale(base, f"{GROUP}-1111111111111111")
    other = stale(base, "bigquery-0000000000000000")
    holding = (
        "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR);"
        " fcntl.flock(fd, fcntl.LOCK_SH); print('held', flush=True); time.sleep(300)"
    )
    holder = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", holding, str(base / f"{held.name}.lock")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline() == "held\n"
        folder = group_env.environment(GROUP, base=base)
    finally:
        holder.kill()
        holder.wait()
    assert group_env.ready(folder)
    assert not unheld.exists()
    assert held.is_dir() and other.is_dir()
    assert (base / f"{unheld.name}.lock").is_file()


def test_a_changed_lock_or_python_version_names_another_environment(tmp_path: Path) -> None:
    for name in group_env.KEYED:
        (tmp_path / name).write_text("one")
    first = group_env.key(GROUP, tmp_path)
    assert first != group_env.key("bigquery", tmp_path)
    for name in group_env.KEYED:
        (tmp_path / name).write_text("two")
        assert group_env.key(GROUP, tmp_path) != first
        (tmp_path / name).write_text("one")
    assert group_env.key(GROUP, tmp_path) == first


def test_a_process_waits_for_the_one_building(tmp_path: Path, cached: None) -> None:
    """A process asking while another holds the lock file exclusively, as the builder does, waits,
    then builds what the other left unbuilt."""
    base, log = tmp_path / "groups", tmp_path / "builds.log"
    base.mkdir()
    fd = os.open(base / f"{group_env.key(GROUP)}.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        waiting = ask(base, log)
        time.sleep(1)
        assert waiting.poll() is None
        assert not log.exists()
    finally:
        os.close(fd)
    out, err = waiting.communicate(timeout=300)
    assert waiting.returncode == 0, err
    assert Path(out.strip()) == base / group_env.key(GROUP)
    assert log.read_text() == "built\n"
