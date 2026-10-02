import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import agent_worker as aw


BASE_POLICY = {
    "repository": "ivan09069/gh-autopilot",
    "default_branch": "master",
    "trigger_label": "agent:ready",
    "allowed_issue_authors": ["ivan09069"],
    "branch_prefix": "agent/",
    "agent": {"command": ["codex", "exec", "--sandbox", "workspace-write", "-"]},
    "limits": {"max_changed_files": 3, "max_changed_lines": 20},
    "protected_paths": [
        ".github/workflows/**",
        "agent_policy.json",
        "*.env",
        "**/secrets/**",
        "**/credentials/**",
    ],
    "pr": {"draft": True},
}


class PolicyTests(unittest.TestCase):
    def test_agent_branch_allowed(self):
        aw.validate_push_branch(BASE_POLICY, "agent/issue-12")

    def test_default_branch_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.validate_push_branch(BASE_POLICY, "master")

    def test_random_branch_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.validate_push_branch(BASE_POLICY, "feature/x")

    def test_protected_path_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.enforce_diff_policy(BASE_POLICY, aw.DiffStats((".github/workflows/ci.yml",), 2))

    def test_policy_file_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.enforce_diff_policy(BASE_POLICY, aw.DiffStats(("agent_policy.json",), 2))

    def test_nested_secret_directory_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.enforce_diff_policy(BASE_POLICY, aw.DiffStats(("src/secrets/token.txt",), 1))

    def test_nested_credentials_directory_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.enforce_diff_policy(BASE_POLICY, aw.DiffStats(("tools/credentials/dev.json",), 1))

    def test_env_file_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.enforce_diff_policy(BASE_POLICY, aw.DiffStats(("prod.env",), 1))

    def test_size_limit_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.enforce_diff_policy(BASE_POLICY, aw.DiffStats(("a.py",), 21))

    def test_safe_diff_allowed(self):
        aw.enforce_diff_policy(BASE_POLICY, aw.DiffStats(("src/a.py", "tests/test_a.py"), 20))

    def test_no_changes_denied(self):
        with self.assertRaises(aw.PolicyError):
            aw.enforce_diff_policy(BASE_POLICY, aw.DiffStats((), 0))

    def test_policy_requires_draft_pr(self):
        bad = dict(BASE_POLICY)
        bad["pr"] = {"draft": False}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "policy.json"
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(aw.PolicyError):
                aw.load_policy(p)

    def test_secret_redaction(self):
        synthetic = "ghp_" + ("A" * 30)
        self.assertNotIn(synthetic, aw.redact(f"token={synthetic}"))

    def test_issue_fingerprint_changes(self):
        a = aw.Issue(1, "x", "a", "ivan09069", "t1", "u")
        b = aw.Issue(1, "x", "b", "ivan09069", "t2", "u")
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_windows_cmd_shim_resolution(self):
        def which(name):
            return r"C:\npm\codex.cmd" if name == "codex.cmd" else None
        with patch("agent_worker.shutil.which", side_effect=which):
            self.assertEqual(aw.resolve_executable("codex", windows=True), r"C:\npm\codex.cmd")

    def test_missing_agent_executable_returns_none(self):
        with patch("agent_worker.shutil.which", return_value=None):
            self.assertIsNone(aw.resolve_executable("codex", windows=True))

    def test_trusted_runner_closes_stdin(self):
        completed = subprocess.CompletedProcess(["x"], 0, "", "")
        with patch("agent_core.subprocess.run", return_value=completed) as mocked:
            aw.run(["x"])
        self.assertIs(mocked.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_trusted_runner_pipes_explicit_input(self):
        completed = subprocess.CompletedProcess(["x"], 0, "", "")
        with patch("agent_core.subprocess.run", return_value=completed) as mocked:
            aw.run(["x"], input_text="multiline\nprompt")
        self.assertEqual(mocked.call_args.kwargs["input"], "multiline\nprompt")
        self.assertIsNone(mocked.call_args.kwargs["stdin"])


if __name__ == "__main__":
    unittest.main()
