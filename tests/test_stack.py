"""The container stack's compose file is valid and keeps heavy services behind their own profiles.

`docker compose config` resolves the file without a running daemon; the tests skip where the
Docker CLI or its compose plugin is absent.
"""

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

COMPOSE = Path(__file__).resolve().parents[1] / "platform/stack/compose.yaml"
DOCKER = shutil.which("docker")


def compose_available() -> bool:
    if DOCKER is None:
        return False
    probe = [DOCKER, "compose", "version"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0


pytestmark = pytest.mark.skipif(not compose_available(), reason="no Docker compose CLI")


def compose(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert DOCKER is not None
    return subprocess.run(
        [DOCKER, "compose", "-f", str(COMPOSE), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def stack_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_LAB_DB_")}
    return env | extra


def resolved(*profiles: str) -> dict[str, Any]:
    # A throwaway password, made per run so no credential is ever written down.
    env = stack_env(LEDGER_LAB_DB_PASSWORD=secrets.token_hex(16))
    flags = [arg for p in profiles for arg in ("--profile", p)]
    result = compose(*flags, "config", "--format", "json", env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_shared_database_is_the_only_service_without_a_profile() -> None:
    assert list(resolved()["services"]) == ["db"]


def test_every_other_service_starts_only_under_its_own_profile() -> None:
    services = resolved("*")["services"]
    for name, service in services.items():
        if name != "db":
            assert service.get("profiles") == [name], name


def test_the_database_is_health_checked_and_bound_to_loopback() -> None:
    db = resolved()["services"]["db"]
    assert "pg_isready" in " ".join(db["healthcheck"]["test"])
    assert all(port["host_ip"] == "127.0.0.1" for port in db["ports"])


def test_the_stack_refuses_to_start_without_a_password_from_the_environment() -> None:
    result = compose("config", env=stack_env())
    assert result.returncode != 0
    assert "LEDGER_LAB_DB_PASSWORD" in result.stderr
