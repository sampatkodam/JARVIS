"""Docker-backed OS isolation boundary for autonomous JARVIS commands."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound

from app.config import WORKSPACE, COMMAND_TIMEOUT, MAX_OUTPUT_CHARS

IMAGE = os.getenv("JARVIS_SANDBOX_IMAGE", "jarvis-sandbox:latest")
MEMORY = os.getenv("JARVIS_SANDBOX_MEMORY", "1g")
CPUS = float(os.getenv("JARVIS_SANDBOX_CPUS", "2.0"))
PIDS_LIMIT = int(os.getenv("JARVIS_SANDBOX_PIDS", "256"))
DEFAULT_NETWORK = os.getenv("JARVIS_SANDBOX_NETWORK", "none")

@dataclass(frozen=True)
class SandboxConfig:
    image: str = IMAGE
    memory: str = MEMORY
    cpus: float = CPUS
    pids_limit: int = PIDS_LIMIT
    network: str = DEFAULT_NETWORK


def docker_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        client.close()
        return True
    except DockerException:
        return False


def image_available(image: str = IMAGE) -> bool:
    try:
        client = docker.from_env()
        client.images.get(image)
        client.close()
        return True
    except (DockerException, ImageNotFound):
        return False


def run_sandboxed(command: str, *, network: str = DEFAULT_NETWORK, config: SandboxConfig | None = None) -> dict:
    """Run a command in an ephemeral, non-privileged container.

    Only the configured workspace is mounted read/write. The root filesystem is
    read-only, /tmp is ephemeral, Linux capabilities are dropped, privilege
    escalation is disabled, process count and memory/CPU are bounded, and network
    access defaults to none. The caller may explicitly request bridge networking
    for dependency installation; that networking still terminates with the
    ephemeral container.
    """
    cfg = config or SandboxConfig(network=network)
    if not docker_available():
        raise RuntimeError("Docker sandbox is unavailable; refusing host execution.")
    if not image_available(cfg.image):
        raise RuntimeError(f"Sandbox image {cfg.image!r} is unavailable.")

    client = docker.from_env()
    try:
        result = client.containers.run(
            cfg.image,
            command=["sh", "-lc", command],
            working_dir="/workspace",
            volumes={str(Path(WORKSPACE).resolve()): {"bind": "/workspace", "mode": "rw"}},
            network_mode=network,
            mem_limit=cfg.memory,
            nano_cpus=int(cfg.cpus * 1_000_000_000),
            pids_limit=cfg.pids_limit,
            read_only=True,
            tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=268435456"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            remove=True,
            stdout=True,
            stderr=True,
            user="10001:10001",
            init=True,
            timeout=COMMAND_TIMEOUT + 10,
        )
        text = result.decode("utf-8", errors="replace")
        return {
            "command": command,
            "return_code": 0,
            "stdout": text[-MAX_OUTPUT_CHARS:],
            "stderr": "",
            "success": True,
            "sandbox": "docker",
            "network": network,
            "memory_limit": cfg.memory,
            "cpu_limit": cfg.cpus,
            "pids_limit": cfg.pids_limit,
        }
    except DockerException as exc:
        return {"command": command, "return_code": 1, "stdout": "", "stderr": str(exc)[-MAX_OUTPUT_CHARS:], "success": False, "sandbox": "docker", "network": network}
    finally:
        client.close()
