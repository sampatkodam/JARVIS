"""Docker-backed OS isolation boundary for autonomous JARVIS commands."""
from __future__ import annotations

import base64
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
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_REKOR_URL_RE = re.compile(r"^https://[^\s/]+(?:/[^\s]*)?$")


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
    if policy.get("version") != 4 or policy.get("verifier") != "cosign" or policy.get("required") is not True:
        raise RuntimeError("Sandbox signing policy must require Cosign verification (version 4).")

    keys = policy.get("trusted_keys")
    if not isinstance(keys, list) or not keys:
        raise RuntimeError("Sandbox signing policy must define at least one trusted signing key.")
    ids = set()
    if policy.get("key_id") is not None and (not isinstance(policy["key_id"], str) or not _KEY_ID_RE.fullmatch(policy["key_id"])):
        raise RuntimeError("Sandbox signing policy has an invalid active signing key ID.")
    active = 0
    for key in keys:
        if not isinstance(key, dict) or not isinstance(key.get("id"), str) or not _KEY_ID_RE.fullmatch(key["id"]) or key["id"] in ids:
            raise RuntimeError("Sandbox signing policy contains an invalid or duplicate signing key ID.")
        ids.add(key["id"])
        if not isinstance(key.get("public_key_env"), str) or not key["public_key_env"]:
            raise RuntimeError(f"Trusted signing key {key['id']!r} does not define a public-key environment variable.")
        if not isinstance(key.get("revoked", False), bool):
            raise RuntimeError(f"Trusted signing key {key['id']!r} has an invalid revoked flag.")
        if not key.get("revoked", False):
            active += 1
    if active == 0:
        raise RuntimeError("Sandbox signing policy has no active trusted signing keys.")
    if policy.get("key_id") is not None and policy["key_id"] not in ids:
        raise RuntimeError(f"Sandbox signing policy references unknown signing key ID {policy['key_id']!r}.")

    tlog = policy.get("transparency_log")
    if not isinstance(tlog, dict) or tlog.get("required") is not True:
        raise RuntimeError("Sandbox signing policy must require Sigstore transparency-log inclusion proofs.")
    rekor_keys = tlog.get("trusted_keys")
    if not isinstance(rekor_keys, list) or not rekor_keys:
        raise RuntimeError("Sandbox signing policy must define at least one trusted Rekor checkpoint key.")
    rekor_ids = set()
    active_rekor = 0
    rekor_key_id = tlog.get("key_id")
    if rekor_key_id is not None and (not isinstance(rekor_key_id, str) or not _KEY_ID_RE.fullmatch(rekor_key_id)):
        raise RuntimeError("Sandbox signing policy has an invalid active Rekor key ID.")
    for key in rekor_keys:
        if not isinstance(key, dict) or not isinstance(key.get("id"), str) or not _KEY_ID_RE.fullmatch(key["id"]) or key["id"] in rekor_ids:
            raise RuntimeError("Sandbox signing policy contains an invalid or duplicate Rekor key ID.")
        rekor_ids.add(key["id"])
        if not isinstance(key.get("public_key_env"), str) or not key["public_key_env"]:
            raise RuntimeError(f"Trusted Rekor key {key['id']!r} does not define a public-key environment variable.")
        if not isinstance(key.get("log_id"), str) or not key["log_id"].strip():
            raise RuntimeError(f"Trusted Rekor key {key['id']!r} must pin a log ID.")
        if not isinstance(key.get("rekor_url"), str) or not _REKOR_URL_RE.fullmatch(key["rekor_url"]):
            raise RuntimeError(f"Trusted Rekor key {key['id']!r} has an invalid Rekor URL.")
        if not isinstance(key.get("checkpoint_origin_prefix"), str) or not key["checkpoint_origin_prefix"].strip():
            raise RuntimeError(f"Trusted Rekor key {key['id']!r} must pin a checkpoint origin prefix.")
        if not isinstance(key.get("revoked", False), bool):
            raise RuntimeError(f"Trusted Rekor key {key['id']!r} has an invalid revoked flag.")
        if not key.get("revoked", False):
            active_rekor += 1
    if active_rekor == 0:
        raise RuntimeError("Sandbox signing policy has no active trusted Rekor checkpoint keys.")
    if rekor_key_id is not None and rekor_key_id not in rekor_ids:
        raise RuntimeError(f"Sandbox signing policy references unknown Rekor key ID {rekor_key_id!r}.")
    return policy


def _decode_b64_hash(value: object, field: str) -> bytes:
    if not isinstance(value, str):
        raise RuntimeError(f"Transparency-log inclusion proof field {field!r} is missing or invalid.")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Transparency-log inclusion proof field {field!r} is not valid base64.") from exc
    if len(decoded) != 32:
        raise RuntimeError(f"Transparency-log inclusion proof field {field!r} must contain a SHA-256 hash.")
    return decoded


def _validate_checkpoint(envelope: object, *, tree_size: int, root_hash: bytes, rekor_key: dict) -> dict:
    if not isinstance(envelope, str) or not envelope.strip():
        raise RuntimeError("Sigstore checkpoint is missing or invalid.")
    lines = envelope.replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 5 or lines[3].strip() != "":
        raise RuntimeError("Sigstore checkpoint format is invalid.")
    origin = lines[0].strip()
    expected_prefix = rekor_key["checkpoint_origin_prefix"]
    if not origin.startswith(expected_prefix):
        raise RuntimeError(f"Unexpected Rekor checkpoint origin: {origin!r}.")
    try:
        checkpoint_tree_size = int(lines[1].strip())
    except ValueError as exc:
        raise RuntimeError("Sigstore checkpoint tree size is invalid.") from exc
    checkpoint_root = _decode_b64_hash(lines[2].strip(), "checkpoint rootHash")
    if checkpoint_tree_size != tree_size or checkpoint_root != root_hash:
        raise RuntimeError("Sigstore checkpoint state does not match the inclusion proof.")
    signature_line = lines[4].strip()
    if not (signature_line.startswith("— ") or signature_line.startswith("- ")):
        raise RuntimeError("Sigstore checkpoint signature is missing or invalid.")
    signature_parts = signature_line[2:].split(None, 1)
    if len(signature_parts) != 2 or not signature_parts[0]:
        raise RuntimeError("Sigstore checkpoint signature identity is invalid.")
    signer_name = signature_parts[0]
    expected_host = rekor_key["rekor_url"].split("//", 1)[1].split("/", 1)[0]
    if signer_name != expected_host:
        raise RuntimeError(f"Unexpected Rekor checkpoint signer: {signer_name!r}.")
    return {"origin": origin, "tree_size": checkpoint_tree_size, "root_hash": checkpoint_root, "signer": signer_name, "rekor_key_id": rekor_key["id"]}


def _validate_proof(proof: object, *, log_id: object, rekor_key: dict) -> dict:
    if not isinstance(proof, dict):
        raise RuntimeError("Sigstore transparency-log inclusion proof is missing or invalid.")
    expected_log_id = rekor_key["log_id"]
    if log_id != expected_log_id:
        raise RuntimeError(f"Unexpected Rekor log ID: {log_id!r}.")
    hashes = proof.get("hashes")
    if not isinstance(hashes, list) or not hashes:
        raise RuntimeError("Sigstore transparency-log inclusion proof hashes are missing or invalid.")
    checkpoint = proof.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise RuntimeError("Sigstore checkpoint is missing or invalid.")
    envelope = checkpoint.get("envelope")
    try:
        log_index = int(proof.get("logIndex"))
        tree_size = int(proof.get("treeSize"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Sigstore transparency-log inclusion proof indexes are invalid.") from exc
    if log_index < 0 or tree_size <= 0 or log_index >= tree_size:
        raise RuntimeError("Sigstore transparency-log inclusion proof indexes are invalid.")
    root_hash = _decode_b64_hash(proof.get("rootHash"), "rootHash")
    decoded_hashes = [_decode_b64_hash(value, "hashes") for value in hashes]
    checkpoint_data = _validate_checkpoint(envelope, tree_size=tree_size, root_hash=root_hash, rekor_key=rekor_key)
    return {
        "log_index": log_index,
        "tree_size": tree_size,
        "root_hash": root_hash,
        "hash_count": len(decoded_hashes),
        "checkpoint": checkpoint_data,
        "log_id": log_id,
        "rekor_key_id": rekor_key["id"],
    }


def verify_inclusion_proof(output: str, *, rekor_key: dict) -> dict:
    """Require inclusion evidence for the exact trusted Rekor key and checkpoint identity/state."""
    try:
        decoded = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Cosign transparency-log verification returned invalid JSON.") from exc

    entries = decoded if isinstance(decoded, list) else [decoded]
    for item in entries:
        if not isinstance(item, dict):
            continue

        materials = item.get("verificationMaterial")
        if isinstance(materials, dict) and isinstance(materials.get("tlogEntries"), list):
            for entry in materials["tlogEntries"]:
                if not isinstance(entry, dict):
                    continue
                log_obj = entry.get("logId") or entry.get("logID")
                log_id = log_obj.get("keyId") if isinstance(log_obj, dict) else log_obj
                return _validate_proof(entry.get("inclusionProof"), log_id=log_id, rekor_key=rekor_key)

        verification = item.get("verification")
        if isinstance(verification, dict) and isinstance(verification.get("inclusionProof"), dict):
            log_id = item.get("logID") or item.get("logId")
            return _validate_proof(verification["inclusionProof"], log_id=log_id, rekor_key=rekor_key)

    raise RuntimeError("Sigstore transparency-log inclusion proof is missing or invalid.")


def _active_rekor_keys(policy: dict) -> list[dict]:
    tlog = policy["transparency_log"]
    keys = {key["id"]: key for key in tlog["trusted_keys"]}
    selected = tlog.get("key_id")
    if selected is not None:
        key = keys[selected]
        return [key] if not key.get("revoked", False) else []
    return [key for key in tlog["trusted_keys"] if not key.get("revoked", False)]


def verify_image_signature_details(image: str, *, policy: dict | None = None, runner=None) -> dict:
    """Verify an image with an active Cosign key and an active trusted Rekor checkpoint key."""
    name, digest = parse_pinned_image(image)
    policy = policy or load_signing_policy()
    keys_by_id = {key["id"]: key for key in policy["trusted_keys"]}
    active_id = policy.get("key_id")
    signing_candidates = [keys_by_id[active_id]] if active_id is not None else [key for key in policy["trusted_keys"] if not key.get("revoked", False)]
    rekor_candidates = _active_rekor_keys(policy)
    if not rekor_candidates:
        selected = policy["transparency_log"].get("key_id")
        raise RuntimeError(f"Sandbox image verification cannot use revoked Rekor key {selected!r}.")

    execute = runner or subprocess.run
    target = f"{name}@sha256:{digest}"
    errors = []
    for key in signing_candidates:
        if key.get("revoked", False):
            errors.append(f"signing key {key['id']} is revoked")
            continue
        key_path = os.getenv(key["public_key_env"])
        if not key_path:
            errors.append(f"signing key {key['id']} is not configured")
            continue
        for rekor_key in rekor_candidates:
            rekor_path = os.getenv(rekor_key["public_key_env"])
            if not rekor_path:
                errors.append(f"Rekor key {rekor_key['id']} is not configured")
                continue
            command = ["cosign", "verify", "--key", key_path, "--rekor-url", rekor_key["rekor_url"], "--output", "json", target]
            verify_env = os.environ.copy()
            verify_env["SIGSTORE_REKOR_PUBLIC_KEY"] = rekor_path
            try:
                result = execute(command, capture_output=True, text=True, check=False, timeout=30, env=verify_env)
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"signing key {key['id']} / Rekor key {rekor_key['id']}: verifier error: {exc}")
                continue
            if getattr(result, "returncode", 1) == 0:
                try:
                    proof = verify_inclusion_proof(getattr(result, "stdout", ""), rekor_key=rekor_key)
                except RuntimeError as exc:
                    errors.append(f"signing key {key['id']} / Rekor key {rekor_key['id']}: {exc}")
                    continue
                return {"signing_key_id": key["id"], "rekor_key_id": rekor_key["id"], "proof": proof}
            detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "verification failed").strip()
            errors.append(f"signing key {key['id']} / Rekor key {rekor_key['id']}: {detail[-300:]}")
    raise RuntimeError("Sandbox image signature/transparency verification failed: " + "; ".join(errors))


def verify_image_signature(image: str, *, policy: dict | None = None, runner=None) -> str:
    """Return the trusted signing key ID after signature and Rekor checkpoint verification."""
    return verify_image_signature_details(image, policy=policy, runner=runner)["signing_key_id"]


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
        provenance = verify_image_signature_details(verified_image)
        result = client.containers.run(verified_image, command=["sh", "-lc", f"timeout {COMMAND_TIMEOUT}s sh -lc {shlex.quote(command)}"], working_dir="/workspace", volumes={str(Path(WORKSPACE).resolve()): {"bind": "/workspace", "mode": "rw"}}, network_mode=selected_network, mem_limit=cfg.memory, nano_cpus=int(cfg.cpus * 1_000_000_000), pids_limit=cfg.pids_limit, read_only=True, tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=268435456"}, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], remove=True, stdout=True, stderr=True, init=True)
        text = result.decode("utf-8", errors="replace")
        return {"command": command, "return_code": 0, "stdout": text[-MAX_OUTPUT_CHARS:], "stderr": "", "success": True, "sandbox": "docker", "image": verified_image, "signature_verified": True, "transparency_log_verified": True, "signing_key_id": provenance["signing_key_id"], "rekor_key_id": provenance["rekor_key_id"], "network": selected_network, "memory_limit": cfg.memory, "cpu_limit": cfg.cpus, "pids_limit": cfg.pids_limit}
    except DockerException as exc:
        return {"command": command, "return_code": 1, "stdout": "", "stderr": str(exc)[-MAX_OUTPUT_CHARS:], "success": False, "sandbox": "docker", "network": selected_network}
    finally:
        client.close()
