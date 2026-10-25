import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/imozuru/scripts/autosave.py"
SKILL = SCRIPT.parent.parent / "SKILL.md"


class AutosaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.env = {**os.environ, "IMOZURU_CONFIG_DIR": str(self.base / "config"),
                    "IMOZURU_STATE_DIR": str(self.base / "state")}
        self.env.pop("CODEX_THREAD_ID", None)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "research")
        self.git("config", "user.name", "Research Test")
        self.git("config", "user.email", "research@example.test")
        self.git("commit", "--allow-empty", "-m", "Initial")
        self.topic = self.base / "topics" / "example"
        self.topic.mkdir(parents=True)
        (self.topic / "map.md").write_text("question\n", encoding="utf-8")

    def git(self, *args, repo=None):
        result = subprocess.run(["git", "-C", str(repo or self.repo), *args],
                                capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def run_helper(self, *args, payload=None, ok=True, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, encoding="utf-8", env=env or self.env)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return json.loads(result.stdout) if ok else result.stderr

    def enable(self, mode="copy", **kwargs):
        args = ["configure", "--mode", mode, "--repo", self.repo]
        if "remote" in kwargs:
            args.extend(["--remote", kwargs["remote"]])
        return self.run_helper(*args)

    def register(self, topic=None):
        result = self.run_helper("register", topic or self.topic)
        return Path(result["destination"])

    def event(self, name, session="one", command=None, success=True):
        event = {"hook_event_name": name, "session_id": session}
        if command:
            event.update(tool_name="Bash", tool_input={"command": command},
                         tool_response={"exit_code": 0 if success else 1})
        return self.run_helper("hook", payload=event)

    def register_event(self, topic=None, session="one", success=True):
        return self.event("PostToolUse", session, shlex.join(["python3", str(SCRIPT), "register", str(topic or self.topic)]), success)

    def test_first_use_and_decline_persist(self):
        self.assertEqual(self.run_helper("status")["mode"], "unset")
        command = shlex.join(["cat", str(SKILL)])
        notice = self.event("PostToolUse", command=command)
        self.assertEqual(notice["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("初回", notice["hookSpecificOutput"]["additionalContext"])
        self.run_helper("configure", "--mode", "off")
        self.assertEqual(self.run_helper("status")["mode"], "off")
        self.assertEqual(self.event("PostToolUse", command=command), {})
        self.assertFalse(self.run_helper("register", self.topic)["registered"])
        self.assertEqual(self.event("Stop"), {})
        self.assertFalse((self.repo / "research").exists())

    def test_hook_requires_successful_direct_read(self):
        for command in (shlex.join(["echo", str(SKILL)]), f"cat {shlex.quote(str(SKILL))}; echo done",
                        shlex.join(["cat", str(SKILL)]) + " | head"):
            self.assertEqual(self.event("PostToolUse", command=command), {})
        self.assertEqual(self.event("PostToolUse", command=shlex.join(["cat", str(SKILL)]), success=False), {})
        self.assertEqual(self.run_helper("hook", payload={"hook_event_name": "PostToolUse", "session_id": "one",
                                                        "tool_name": "Bash", "tool_input": {"command": f"cat {SKILL}"}, "tool_response": "unknown"}), {})
        self.assertIn("hookSpecificOutput", self.event("PostToolUse", command=shlex.join(["rtk", "proxy", "sed", "-n", "1,50p", str(SKILL)])))
        self.assertEqual(self.event("PostToolUse", command=shlex.join(["python3", str(SCRIPT), "status"])), {})
        self.assertIn("hookSpecificOutput", self.run_helper("hook", payload={"hook_event_name": "PostToolUse", "session_id": "two", "tool_name": "Read", "tool_input": {"file_path": str(SKILL)}, "tool_response": {"content": "skill"}}))

    def test_register_session_binding_and_idempotent_stop(self):
        self.enable()
        dest = self.register()
        self.assertEqual(self.event("Stop"), {})
        self.assertFalse(dest.exists())
        self.register_event(success=False)
        self.event("Stop")
        self.assertFalse(dest.exists())
        self.register_event()
        self.event("Stop", session="two")
        self.assertFalse(dest.exists())
        self.event("Stop")
        out = dest / "map.md"
        self.assertEqual(out.read_text(encoding="utf-8"), "question\n")
        modified = out.stat().st_mtime_ns
        self.event("Stop")
        self.assertEqual(out.stat().st_mtime_ns, modified)
        self.assertEqual(self.run_helper("sync", self.topic)["copied"], 0)

    def test_copy_allowlist_and_no_mirror_deletes(self):
        self.enable()
        dest = self.register()
        for name in ("sources/private.md", "report.html", "digest.html", ".env", "unknown.md", "watch/day.md"):
            path = self.topic / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
        (self.topic / "log.md").symlink_to(self.topic / "sources/private.md")
        self.run_helper("sync", self.topic)
        self.assertEqual({p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}, {"map.md", "watch/day.md"})
        (self.topic / "map.md").unlink()
        self.run_helper("sync", self.topic)
        self.assertTrue((dest / "map.md").exists())

    def test_same_name_topics_are_distinct(self):
        self.enable()
        other = self.base / "other" / "example"
        other.mkdir(parents=True)
        self.assertNotEqual(self.register(), self.register(other))

    def test_user_edits_and_unknown_destination_are_preserved(self):
        self.enable()
        dest = self.register()
        dest.mkdir(parents=True)
        (dest / "map.md").write_text("user", encoding="utf-8")
        self.assertIn("unknown or user-edited", self.run_helper("sync", self.topic, ok=False))
        self.assertEqual((dest / "map.md").read_text(encoding="utf-8"), "user")
        (dest / "map.md").unlink()
        self.run_helper("sync", self.topic)
        (dest / "map.md").write_text("edited", encoding="utf-8")
        self.register_event()
        failure = self.event("Stop")
        self.assertIn("systemMessage", failure)
        self.assertNotIn("decision", failure)
        self.assertEqual((self.topic / "map.md").read_text(encoding="utf-8"), "question\n")
        self.assertEqual((dest / "map.md").read_text(encoding="utf-8"), "edited")

    def test_path_boundaries(self):
        self.enable()
        self.run_helper("register", self.repo, ok=False)
        self.register()
        state_path = self.base / "state/autosave.json"
        value = json.loads(state_path.read_text(encoding="utf-8"))
        value["topics"][str(self.topic)]["destination"] = "../escape"
        state_path.write_text(json.dumps(value), encoding="utf-8")
        self.assertIn("invalid registered", self.run_helper("sync", self.topic, ok=False))
        self.assertFalse((self.base / "escape").exists())

    def test_destination_symlink_refused(self):
        self.enable()
        dest = self.register()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.repo / "research").symlink_to(elsewhere, target_is_directory=True)
        self.assertIn("symlink", self.run_helper("sync", self.topic, ok=False))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_hooks_install_preserves_existing_and_is_idempotent(self):
        path = self.base / "hooks.json"
        existing = {"other": "keep", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}}
        path.write_text(json.dumps(existing), encoding="utf-8")
        self.assertFalse(self.run_helper("install-hooks", path)["applied"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), existing)
        first = self.run_helper("install-hooks", path, "--apply")
        second = self.run_helper("install-hooks", path, "--apply")
        self.assertEqual(first, second)
        self.assertEqual(second["config"]["other"], "keep")
        self.assertEqual(len(second["config"]["hooks"]["Stop"]), 2)
        self.assertEqual(len(second["config"]["hooks"]["PostToolUse"]), 1)
        self.assertIn(str(SCRIPT), second["config"]["hooks"]["Stop"][1]["hooks"][0]["command"])

    def test_invalid_configuration_refuses_writes(self):
        self.enable()
        self.register()
        path = self.base / "config/autosave.json"
        path.write_text('{"version": 1, "mode": "surprise"}', encoding="utf-8")
        self.assertIn("invalid autosave", self.run_helper("sync", self.topic, ok=False))
        self.assertFalse((self.repo / "research").exists())

    def test_commit_mode_and_dirty_target_refusal(self):
        self.enable("commit")
        dest = self.register()
        first = self.run_helper("sync", self.topic)
        self.assertTrue(first["committed"])
        self.assertFalse(first["pushed"])
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.run_helper("sync", self.topic)["copied"], 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        (self.repo / "unrelated.txt").write_text("keep", encoding="utf-8")
        self.git("add", "unrelated.txt")
        (self.topic / "map.md").write_text("updated", encoding="utf-8")
        self.assertIn("clean", self.run_helper("sync", self.topic, ok=False))
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "unrelated.txt")
        self.assertEqual((dest / "map.md").read_text(encoding="utf-8"), "question\n")

    def test_develop_and_changed_branch_refused(self):
        self.git("checkout", "-b", "develop")
        self.assertIn("develop", self.run_helper("configure", "--mode", "commit", "--repo", self.repo, ok=False))
        self.git("checkout", "research")
        self.enable("commit")
        self.register()
        self.git("checkout", "develop")
        self.assertIn("develop", self.run_helper("sync", self.topic, ok=False))
        self.assertFalse((self.repo / "research").exists())

    def test_push_to_explicit_local_remote_and_failure(self):
        remote = self.base / "remote.git"
        result = subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.git("remote", "add", "archive", str(remote))
        self.enable("push", remote="archive")
        self.register()
        self.assertTrue(self.run_helper("sync", self.topic)["pushed"])
        self.assertEqual(self.git("rev-parse", "refs/heads/research", repo=remote), self.git("rev-parse", "HEAD"))
        reject = remote / "hooks" / "pre-receive"
        reject.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        reject.chmod(0o755)
        (self.topic / "map.md").write_text("next", encoding="utf-8")
        self.run_helper("sync", self.topic, ok=False)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("unresolved previous Git save", self.run_helper("sync", self.topic, ok=False))
        self.assertTrue(self.run_helper("status")["pending_git"])

    def test_changed_remote_is_refused_before_copy(self):
        self.git("remote", "add", "archive", str(self.base / "approved.git"))
        self.enable("push", remote="archive")
        dest = self.register()
        self.git("remote", "set-url", "archive", str(self.base / "unapproved.git"))
        self.assertIn("destination changed", self.run_helper("sync", self.topic, ok=False))
        self.assertFalse(dest.exists())

    def test_failed_commit_is_not_silently_retried(self):
        self.enable("commit")
        self.register()
        reject = self.repo / ".git/hooks/pre-commit"
        reject.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        reject.chmod(0o755)
        self.run_helper("sync", self.topic, ok=False)
        head = self.git("rev-parse", "HEAD")
        self.assertIn("unresolved previous Git save", self.run_helper("sync", self.topic, ok=False))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)


if __name__ == "__main__":
    unittest.main()
