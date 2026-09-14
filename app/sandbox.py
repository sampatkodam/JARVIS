"""Docker-backed OS isolation boundary for autonomous JARVIS commands."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound

from app.config import WORKSPACE, COMMAND_TIMEOUT, MAX_OUTPUT_CHARS

IMAGE = os.getenv("JARVIS_SANDBOX_IMAGE", "")
MEMORY = os.getenv("JARVIS_SANDBOX_MEMORY", "1g")
CPUS = float(os.getenv("JARVIS_SANDBOX_CPUS", "2.0"))
PIDS_LIMIT = int(os.getenv("JARVIS_SANDBOX_PIDS", "256"))
DEFAULT_NETWORK = os.getenv("JARVIS_SANDBOX_NETWORK", "none")
SIGNING_POLICY = os.getenv("JARVIS_SANDBOX_SIGNING_POLICY", "config/sandbox-signing-policy.json")
_DIGEST_RE = re.compile(r"^(?P<name>.+)@sha256:(?P<digest>[0-9a-fA-F]{64})$")

@dataclass(frozen=True)
class SandboxConfig:
    image: str = IMAGE
    memory: str = MEMORY
    cpus: float = CPUS
    pids_limit: int = PIDS_LIMIT
    network: str = DEFAULT_NETWORK


def parse_pinned_image(image: str) -> tuple[str, str]:
    match = _DIGEST_RE.fullmatch(image.strip())
    if not match:
        raise ValueError("Sandbox image must be digest-pinned as <image>@sha256:<64-hex-digest>; mutable tags are forbidden.")
    return match.group("name"), match.group("digest").lower()


def load_signing_policy(path: str = SIGNING_POLICY) -> dict:
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Sandbox signing policy is unavailable or invalid: {path!r}.") from exc
    if policy.get("version") != 1 or policy.get("verifier") != "cosign" or policy.get("required") is not True:
        raise RuntimeError("Sandbox signing policy must require Cosign verification (version 1).")
    key_env = policy.get("public_key_env")
    if not isinstance(key_env, str) or not key_env:
        raise RuntimeError("Sandbox signing policy does not define a trusted public-key environment variable.")
    return policy


def verify_image_signature(image: str, *, policy: dict | None = None, runner=None) -> None:
    """Fail closed unless Cosign verifies the exact digest-pinned image with the trusted key."""
    name, digest = parse_pinned_image(image)
    policy = policy or load_signing_policy()
    key_env = policy["public_key_env"]
    key_path = os.getenv(key_env)
    if not key_path:
        raise RuntimeError(f"Trusted sandbox signing key is not configured ({key_env}).")
    command = ["cosign", "verify", "--key", key_path, f"{name}@sha256:{digest}"]
    execute = runner or subprocess.run
    try:
        result = execute(command, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Cosign verification could not be executed; refusing sandbox execution.") from exc
    if getattr(result, "returncode", 1) != 0:
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "verification failed").strip()
        raise RuntimeError(f"Sandbox image signature verification failed: {detail[-500:]}")


def verify_image_digest(client, image: str) -> str:
    name, expected = parse_pinned_image(image)
    try:
        info = client.images.get(image)
    except (DockerException, ImageNotFound) as exc:
        raise RuntimeError(f"Pinned sandbox image {image!r} is unavailable.") from exc
    repo_digests = {value.lower() for value in (info.attrs.get("RepoDigests") or [])}
    expected_ref = f"{name}@sha256:{expected}".lower()
    if expected_ref not in repo_digests:
        raise RuntimeError(f"Sandbox image digest mismatch: expected {expected_ref}, found {sorted(repo_digests) or 'no repository digest'}.")
    return expected_ref


def dependency_network(command: str) -> str:
    try:
        tokens = [t.lower() for t in shlex.split(command, posix=True)]
    except ValueError:
        return DEFAULT_NETWORK
    if not tokens:
        return DEFAULT_NETWORK
    exe = Path(tokens[0]).name
    if exe in {"pip", "pip3"} and "install" in tokens[1:]: return "bridge"
    if exe in {"npm", "pnpm", "yarn"} and tokens[1:2] == ["install"]: return "bridge"
    if exe in {"python", "python3"} and tokens[1:4] == ["-m", "pip", "install"]: return "bridge"
    if exe == "uv" and len(tokens) >= 3 and tokens[1:3] == ["pip", "install"]: return "bridge"
    return DEFAULT_NETWORK


def docker_available() -> bool:
    try:
        client = docker.from_env(); client.ping(); client.close(); return True
    except DockerException: return False


def image_available(image: str = IMAGE) -> bool:
    try:
        client = docker.from_env(); verify_image_digest(client, image); verify_image_signature(image); client.close(); return True
    except (DockerException, ImageNotFound, ValueError, RuntimeError): return False


def run_sandboxed(command: str, *, network: str | None = None, config: SandboxConfig | None = None) -> dict:
    cfg = config or SandboxConfig()
    selected_network = network or dependency_network(command)
    if not docker_available(): raise RuntimeError("Docker sandbox is unavailable; refusing host execution.")
    client = docker.from_env()
    try:
        verified_image = verify_image_digest(client, cfg.image)
        verify_image_signature(verified_image)
        result = client.containers.run(
            verified_image,
            command=["sh", "-lc", f"timeout {COMMAND_TIMEOUT}s sh -lc {shlex.quote(command)}"],
            working_dir="/workspace",
            volumes={str(Path(WORKSPACE).resolve()): {"bind": "/workspace", "mode": "rw"}},
            network_mode=selected_network,
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
            init=True,
        )
        text = result.decode("utf-8", errors="replace")
        return {"command": command, "return_code": 0, "stdout": text[-MAX_OUTPUT_CHARS:], "stderr": "", "success": True, "sandbox": "docker", "image": verified_image, "signature_verified": True, "network": selected_network, "memory_limit": cfg.memory, "cpu_limit": cfg.cpus, "pids_limit": cfg.pids_limit}
    except DockerException as exc:
        return {"command": command, "return_code": 1, "stdout": "", "stderr": str(exc)[-MAX_OUTPUT_CHARS:], "success": False, "sandbox": "docker", "network": selected_network}
    finally:
        client.close()
