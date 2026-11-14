"""The container stack: its compose file, checked offline, and its running database, checked live.

The offline tests resolve the file with `docker compose config`, which needs no running daemon.
The live test is marked `stack` and kept out of `make check`; `make stack-test` starts this
checkout's stack and runs it against the real server.
Every test skips where the Docker CLI or its compose plugin is absent.
"""

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / "platform/stack/compose.yaml"
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
        [DOCKER, "compose", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def stack_env(**extra: str) -> dict[str, str]:
    """The caller's environment without any stack or Compose setting, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LEDGER_LAB_DB_", "COMPOSE_"))}
    return env | extra


def resolved(*profiles: str) -> dict[str, Any]:
    # A throwaway password, made per run so no credential is ever written down.
    env = stack_env(LEDGER_LAB_DB_PASSWORD=secrets.token_hex(16))
    flags = [arg for p in profiles for arg in ("--profile", p)]
    result = compose("-f", str(COMPOSE), *flags, "config", "--format", "json", env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def planned_databases(config: dict[str, Any]) -> list[str]:
    """The per-service databases the stack declares, as the server's init script reads them."""
    return config["services"]["db"]["environment"]["LEDGER_LAB_DATABASES"].split()


def test_the_shared_database_is_the_only_service_without_a_profile() -> None:
    assert list(resolved()["services"]) == ["db"]


def test_every_other_service_starts_only_under_its_own_profile() -> None:
    services = resolved("*")["services"]
    for name, service in services.items():
        if name != "db":
            assert service.get("profiles") == [name], name


def test_the_database_is_health_checked_over_tcp_and_bound_to_loopback() -> None:
    db = resolved()["services"]["db"]
    # Over TCP, because the image's first-start server listens on its socket only while the init
    # scripts run: a socket check would call the server ready before its databases exist.
    check = " ".join(db["healthcheck"]["test"])
    assert "pg_isready" in check
    assert "-h 127.0.0.1" in check
    assert all(port["host_ip"] == "127.0.0.1" for port in db["ports"])


def test_the_stack_declares_one_database_per_planned_service() -> None:
    names = planned_databases(resolved())
    assert names
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names), names


def test_the_stack_refuses_to_start_without_a_password_from_the_environment() -> None:
    result = compose("-f", str(COMPOSE), "config", env=stack_env())
    assert result.returncode != 0
    assert "LEDGER_LAB_DB_PASSWORD" in result.stderr


@pytest.mark.stack
def test_the_running_server_holds_one_database_per_planned_service() -> None:
    # `make stack-test` names this checkout's stack through COMPOSE_FILE, COMPOSE_PROJECT_NAME and
    # COMPOSE_ENV_FILES, so every call below reaches the same project the Makefile started.
    env = dict(os.environ)
    if "COMPOSE_PROJECT_NAME" not in env:
        pytest.fail("run the live stack test through `make stack-test`")
    config = compose("config", "--format", "json", env=env)
    assert config.returncode == 0, config.stderr
    planned = planned_databases(json.loads(config.stdout))

    published = compose("port", "db", "5432", env=env)
    assert published.returncode == 0, published.stderr
    host, _, port = published.stdout.strip().rpartition(":")
    assert host == "127.0.0.1"
    with socket.create_connection((host, int(port)), timeout=5):
        pass

    # A one-off client on the stack's network signs in over TCP with the stack's own credentials,
    # as the services will; the server's host rule asks for the password there.
    query = "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname"
    client = 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h db -U "$POSTGRES_USER" -d postgres -Atc "$0"'
    listed = compose("run", "--rm", "--no-deps", "-T", "db", "sh", "-c", client, query, env=env)
    assert listed.returncode == 0, listed.stderr
    present = set(listed.stdout.split())
    assert [name for name in planned if name not in present] == []
