"""The worker count `make check`'s `-n auto` starts (`tests/workers.py`): the CPUs, capped by the
memory available, and the environment's count when one is set."""

import os
import subprocess
import sys
from pathlib import Path

from tests.workers import WORKER_MEMORY, auto_workers, available_memory

GIB = 1 << 30


def test_a_many_cpu_low_memory_machine_gets_one_worker_a_gibibyte() -> None:
    # The machine that froze: 28 CPUs and 15 GB, of which about 12 GB available to new work.
    assert auto_workers(28, 12 * GIB) == 12
    assert auto_workers(28, 12 * GIB + GIB // 2) == 12


def test_plenty_of_memory_leaves_one_worker_per_cpu() -> None:
    assert auto_workers(28, 64 * GIB) == 28
    assert auto_workers(4, 31 * GIB) == 4


def test_unknown_or_tiny_memory_still_runs_one_worker_at_least() -> None:
    assert auto_workers(8, None) == 8
    assert auto_workers(8, GIB // 2) == 1
    assert auto_workers(0, None) == 1


def test_the_available_memory_is_read_from_meminfo(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       32000000 kB\nMemAvailable:   12582912 kB\n")
    assert available_memory(meminfo) == 12 * GIB
    meminfo.write_text("MemTotal:       32000000 kB\n")
    assert available_memory(meminfo) is None
    assert available_memory(tmp_path / "missing") is None
    assert WORKER_MEMORY == GIB


def run_auto(env: dict[str, str], tmp_path: Path, memory: int | None = None) -> str:
    """A child pytest at `-n auto` over one trivial test, with its worker line shown; with
    `memory`, the child's read of the available memory answers that, through a plugin."""
    test = Path(__file__).with_name("test_workers.py")
    plugin = []
    if memory is not None:
        (tmp_path / "fixed_memory.py").write_text(
            f"import tests.workers\ntests.workers.available_memory = lambda *_: {memory}\n"
        )
        env = {**env, "PYTHONPATH": os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")])}
        plugin = ["-p", "fixed_memory"]
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            *plugin,
            "-n",
            "auto",
            "-v",
            "-p",
            "no:cacheprovider",
            f"{test}::test_plenty_of_memory_leaves_one_worker_per_cpu",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.stdout


def unset() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "PYTEST_XDIST_AUTO_NUM_WORKERS"}


def test_the_environments_count_overrides_the_cap(tmp_path: Path) -> None:
    env = {**os.environ, "PYTEST_XDIST_AUTO_NUM_WORKERS": "3"}
    assert "created: 3/3 workers" in run_auto(env, tmp_path, memory=1 << 30)


def test_with_no_count_set_auto_starts_the_count_the_memory_allows(tmp_path: Path) -> None:
    from tests.workers import usable_cpus

    expected = min(usable_cpus(), 2)
    assert f"created: {expected}/{expected} workers" in run_auto(unset(), tmp_path, memory=2 * GIB)


def test_a_count_that_is_not_a_number_keeps_the_cap(tmp_path: Path) -> None:
    from tests.workers import count_set, usable_cpus

    assert not count_set({"PYTEST_XDIST_AUTO_NUM_WORKERS": "abc"})
    assert count_set({"PYTEST_XDIST_AUTO_NUM_WORKERS": "3"})
    assert not count_set({})
    env = {**unset(), "PYTEST_XDIST_AUTO_NUM_WORKERS": "abc"}
    expected = min(usable_cpus(), 2)
    assert f"created: {expected}/{expected} workers" in run_auto(env, tmp_path, memory=2 * GIB)


def test_a_run_without_xdist_still_starts() -> None:
    test = Path(__file__).with_name("test_workers.py")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:xdist",
            "-p",
            "no:cacheprovider",
            f"{test}::test_plenty_of_memory_leaves_one_worker_per_cpu",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-400:]
