import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import sandbox


class SandboxImageProvenanceTests(unittest.TestCase):
    DIGEST = "a" * 64
    IMAGE = f"registry.example/jarvis-sandbox@sha256:{DIGEST}"

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
        client.images.get.return_value.attrs = {
            "RepoDigests": [f"registry.example/jarvis-sandbox@sha256:{'b' * 64}"]
        }
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            sandbox.verify_image_digest(client, self.IMAGE)

    def test_matching_digest_is_accepted(self):
        client = Mock()
        client.images.get.return_value.attrs = {"RepoDigests": [self.IMAGE]}
        self.assertEqual(sandbox.verify_image_digest(client, self.IMAGE), self.IMAGE)

    @staticmethod
    def signing_policy():
        return {"version": 1, "verifier": "cosign", "required": True, "public_key_env": "TEST_COSIGN_KEY"}

    def test_unsigned_image_is_rejected(self):
        runner = Mock(return_value=Mock(returncode=1, stdout="", stderr="no matching signatures"))
        with patch.dict(os.environ, {"TEST_COSIGN_KEY": "trusted.pub"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "signature verification failed"):
                sandbox.verify_image_signature(self.IMAGE, policy=self.signing_policy(), runner=runner)
        runner.assert_called_once_with(
            ["cosign", "verify", "--key", "trusted.pub", self.IMAGE],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_invalid_signature_is_rejected(self):
        runner = Mock(return_value=Mock(returncode=1, stdout="", stderr="invalid signature"))
        with patch.dict(os.environ, {"TEST_COSIGN_KEY": "trusted.pub"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "invalid signature"):
                sandbox.verify_image_signature(self.IMAGE, policy=self.signing_policy(), runner=runner)

    def test_valid_signature_is_accepted(self):
        runner = Mock(return_value=Mock(returncode=0, stdout="verified", stderr=""))
        with patch.dict(os.environ, {"TEST_COSIGN_KEY": "trusted.pub"}, clear=False):
            sandbox.verify_image_signature(self.IMAGE, policy=self.signing_policy(), runner=runner)
        runner.assert_called_once_with(
            ["cosign", "verify", "--key", "trusted.pub", self.IMAGE],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )


@unittest.skipUnless(sandbox.docker_available(), "Docker daemon unavailable")
class SandboxIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not sandbox.image_available():
            raise unittest.SkipTest("JARVIS sandbox image unavailable, digest-pinned, or signed")
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
