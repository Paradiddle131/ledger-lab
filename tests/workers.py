"""How many pytest-xdist workers `-n auto` starts: one per CPU this process may use, but no more
than the memory available holds at about a gibibyte a worker, so a machine with many CPUs and
little memory does not run out of it. `PYTEST_XDIST_AUTO_NUM_WORKERS` set to a count overrides
both, as xdist's own reading of it does."""

import os
from collections.abc import Mapping
from pathlib import Path

# The most one worker's processes took at once on the suite, rounded up: 28 workers peaked at
# 15.8 GB together, with each worker's largest process at about 1 GB.
WORKER_MEMORY = 1 << 30
VARIABLE = "PYTEST_XDIST_AUTO_NUM_WORKERS"


def available_memory(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """The memory the kernel says new work can take without swapping, in bytes; None where it
    does not say (no `/proc/meminfo`, as on macOS)."""
    try:
        lines = meminfo.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        name, _, value = line.partition(":")
        if name == "MemAvailable":
            return int(value.split()[0]) * 1024
    return None


def usable_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def auto_workers(cpus: int, available: int | None) -> int:
    """The workers `-n auto` starts: the CPUs, capped by the memory available when it is known."""
    if available is None:
        return max(1, cpus)
    return max(1, min(cpus, available // WORKER_MEMORY))


def count_set(env: Mapping[str, str]) -> bool:
    """Whether the environment sets a worker count xdist will use: a whole number. Any other value
    xdist ignores, so the memory cap still applies."""
    try:
        int(env.get(VARIABLE, ""))
    except ValueError:
        return False
    return True
