import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import sandbox


class SandboxImageProvenanceTests(unittest.TestCase):
    DIGEST = "a" * 64
    IMAGE = f"registry.example/jarvis-sandbox@sha256:{DIGEST}"
    ROOT_HASH = base64.b64encode(b"r" * 32).decode()
    PROOF_HASH = base64.b64encode(b"h" * 32).decode()

    def test_mutable_tag_is_rejected(self):
        for image in ("jarvis-sandbox:latest", "jarvis-sandbox:v1", "python:3.11-slim"):
            with self.assertRaises(ValueError):
                sandbox.parse_pinned_image(image)

    def test_digest_reference_is_parsed(self):
        name, digest = sandbox.parse_pinned_image(self.IMAGE)
        self.assertEqual(name, "registry.example/jarvis-sandbox")
        self.assertEqual(digest, self.DIGEST)

    def test_image_mismatch_is_rejected(self):
        client = Mock()
        client.images.get.return_value.attrs = {"RepoDigests": [f"registry.example/jarvis-sandbox@sha256:{'b' * 64}"]}
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            sandbox.verify_image_digest(client, self.IMAGE)

    def test_matching_digest_is_accepted(self):
        client = Mock()
        client.images.get.return_value.attrs = {"RepoDigests": [self.IMAGE]}
        self.assertEqual(sandbox.verify_image_digest(client, self.IMAGE), self.IMAGE)

    @classmethod
    def policy(cls):
        return {"version": 3, "verifier": "cosign", "required": True, "trusted_keys": [
            {"id": "key-a", "public_key_env": "TEST_KEY_A", "revoked": False},
            {"id": "key-b", "public_key_env": "TEST_KEY_B", "revoked": False},
        ], "transparency_log": {"required": True, "rekor_url": "https://rekor.sigstore.dev"}}

    @classmethod
    def valid_cosign_output(cls):
        return json.dumps([{
            "verificationMaterial": {
                "tlogEntries": [{
                    "logIndex": "125680200",
                    "inclusionProof": {
                        "logIndex": "3775938",
                        "rootHash": cls.ROOT_HASH,
                        "treeSize": "3775939",
                        "hashes": [cls.PROOF_HASH],
                        "checkpoint": {"envelope": "rekor.sigstore.dev - signed checkpoint"},
                    },
                }]
            }
        }])

    def test_rotation_accepts_new_key_when_old_key_is_revoked(self):
        runner = Mock(side_effect=[
            Mock(returncode=0, stdout=self.valid_cosign_output(), stderr="")
        ])
        with patch.dict(os.environ, {"TEST_KEY_A": "old.pub", "TEST_KEY_B": "new.pub"}, clear=False):
            policy = self.policy()
            policy["trusted_keys"][0]["revoked"] = True
            self.assertEqual(sandbox.verify_image_signature(self.IMAGE, policy=policy, runner=runner), "key-b")
        runner.assert_called_once_with(["cosign", "verify", "--key", "new.pub", "--rekor-url", "https://rekor.sigstore.dev", "--output", "json", self.IMAGE], capture_output=True, text=True, check=False, timeout=30)

    def test_revoked_key_is_never_invoked(self):
        runner = Mock(return_value=Mock(returncode=0, stdout=self.valid_cosign_output(), stderr=""))
        with patch.dict(os.environ, {"TEST_KEY_A": "revoked.pub", "TEST_KEY_B": "new.pub"}, clear=False):
            policy = self.policy()
            policy["trusted_keys"][0]["revoked"] = True
            self.assertEqual(sandbox.verify_image_signature(self.IMAGE, policy=policy, runner=runner), "key-b")
        runner.assert_called_once_with(["cosign", "verify", "--key", "new.pub", "--rekor-url", "https://rekor.sigstore.dev", "--output", "json", self.IMAGE], capture_output=True, text=True, check=False, timeout=30)

    def test_explicit_revoked_key_id_is_rejected(self):
        runner = Mock()
        with patch.dict(os.environ, {"TEST_KEY_A": "revoked.pub"}, clear=False):
            policy = self.policy()
            policy["key_id"] = "key-a"
            policy["trusted_keys"][0]["revoked"] = True
            with self.assertRaisesRegex(RuntimeError, "revoked"):
                sandbox.verify_image_signature(self.IMAGE, policy=policy, runner=runner)
        runner.assert_not_called()

    def test_explicit_key_id_selects_rotation_target(self):
        runner = Mock(return_value=Mock(returncode=0, stdout=self.valid_cosign_output(), stderr=""))
        with patch.dict(os.environ, {"TEST_KEY_A": "old.pub", "TEST_KEY_B": "new.pub"}, clear=False):
            policy = self.policy()
            policy["key_id"] = "key-b"
            self.assertEqual(sandbox.verify_image_signature(self.IMAGE, policy=policy, runner=runner), "key-b")
        runner.assert_called_once_with(["cosign", "verify", "--key", "new.pub", "--rekor-url", "https://rekor.sigstore.dev", "--output", "json", self.IMAGE], capture_output=True, text=True, check=False, timeout=30)

    def test_no_active_keys_is_invalid_policy(self):
        policy = self.policy()
        for key in policy["trusted_keys"]:
            key["revoked"] = True
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(policy, f)
            path = f.name
        try:
            with self.assertRaisesRegex(RuntimeError, "no active trusted keys"):
                sandbox.load_signing_policy(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_valid_signature_and_inclusion_proof_are_accepted(self):
        runner = Mock(return_value=Mock(returncode=0, stdout=self.valid_cosign_output(), stderr=""))
        with patch.dict(os.environ, {"TEST_KEY_A": "trusted.pub"}, clear=False):
            policy = self.policy()
            policy["trusted_keys"] = [policy["trusted_keys"][0]]
            self.assertEqual(sandbox.verify_image_signature(self.IMAGE, policy=policy, runner=runner), "key-a")

    def test_missing_inclusion_proof_fails_closed(self):
        runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps([{
            "verificationMaterial": {"tlogEntries": []}
        }]), stderr=""))
        with patch.dict(os.environ, {"TEST_KEY_A": "trusted.pub"}, clear=False):
            policy = self.policy()
            policy["trusted_keys"] = [policy["trusted_keys"][0]]
            with self.assertRaisesRegex(RuntimeError, "inclusion proof is missing or invalid"):
                sandbox.verify_image_signature(self.IMAGE, policy=policy, runner=runner)

    def test_invalid_inclusion_proof_fails_closed(self):
        invalid = json.loads(self.valid_cosign_output())
        invalid[0]["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]["rootHash"] = "not-base64"
        runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps(invalid), stderr=""))
        with patch.dict(os.environ, {"TEST_KEY_A": "trusted.pub"}, clear=False):
            policy = self.policy()
            policy["trusted_keys"] = [policy["trusted_keys"][0]]
            with self.assertRaisesRegex(RuntimeError, "inclusion proof is missing or invalid"):
                sandbox.verify_image_signature(self.IMAGE, policy=policy, runner=runner)

    def test_invalid_cosign_json_fails_closed(self):
        runner = Mock(return_value=Mock(returncode=0, stdout="not-json", stderr=""))
        with patch.dict(os.environ, {"TEST_KEY_A": "trusted.pub"}, clear=False):
            policy = self.policy()
            policy["trusted_keys"] = [policy["trusted_keys"][0]]
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                sandbox.verify_image_signature(self.IMAGE, policy=policy, runner=runner)

    def test_policy_requires_transparency_log(self):
        policy = self.policy()
        del policy["transparency_log"]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(policy, f)
            path = f.name
        try:
            with self.assertRaisesRegex(RuntimeError, "transparency-log inclusion proofs"):
                sandbox.load_signing_policy(path)
        finally:
            Path(path).unlink(missing_ok=True)


@unittest.skipUnless(sandbox.docker_available(), "Docker daemon unavailable")
class SandboxIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not sandbox.image_available():
            raise unittest.SkipTest("JARVIS sandbox image unavailable, digest-pinned, signed, or transparency-log verified")
        cls.tmp = tempfile.TemporaryDirectory()
        sandbox.WORKSPACE = Path(cls.tmp.name).resolve()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def execute_sandbox(self, command):
        result = sandbox.run_sandboxed(command)
        self.assertEqual(result["sandbox"], "docker")
        return result

    def test_workspace_write_is_visible(self):
        result = self.execute_sandbox("python -c \"open('sandbox-test.txt','w').write('ok')\"")
        self.assertTrue(result["success"], result)
        self.assertEqual((sandbox.WORKSPACE / "sandbox-test.txt").read_text(), "ok")

    def test_host_filesystem_is_not_writable(self):
        result = self.execute_sandbox("sh -c 'echo blocked > /escape.txt'")
        self.assertFalse(result["success"])
        self.assertFalse((sandbox.WORKSPACE.parent / "escape.txt").exists())

    def test_network_is_disabled_by_default(self):
        result = self.execute_sandbox("python -c \"import socket; socket.create_connection(('1.1.1.1',80),1)\"")
        self.assertFalse(result["success"], result)
        self.assertEqual(result["network"], "none")

    def test_process_and_memory_limits_are_present(self):
        result = self.execute_sandbox("python -c \"print(open('/sys/fs/cgroup/pids.max').read().strip()); print(open('/sys/fs/cgroup/memory.max').read().strip())\"")
        self.assertTrue(result["success"], result)
        lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2)
        self.assertEqual(lines[0], str(sandbox.PIDS_LIMIT))
        self.assertEqual(lines[1], str(1024**3))

    def test_dependency_network_is_explicit(self):
        self.assertEqual(sandbox.dependency_network("pip install requests"), "bridge")
        self.assertEqual(sandbox.dependency_network("npm install"), "bridge")
        self.assertEqual(sandbox.dependency_network("python -m unittest discover -s tests"), "none")


if __name__ == "__main__":
    unittest.main()
