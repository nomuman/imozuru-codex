#!/usr/bin/env python3
"""Consent-based research snapshots and optional Codex hooks."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


SCRIPT = Path(__file__).resolve()
SKILL = SCRIPT.parent.parent / "SKILL.md"
ARTIFACTS = ("map.md", "log.md", "extracts.json", "digest.json", "queries.jsonl",
             "fetch-log.jsonl", "watch.tsv")
MODES = ("off", "copy", "commit", "push")


def directory(kind):
    override = os.environ.get(f"IMOZURU_{kind}_DIR")
    base = os.environ.get(f"XDG_{kind}_HOME")
    default = ".config" if kind == "CONFIG" else ".local/state"
    return Path(override or str(Path(base or Path.home() / default) / "imozuru")).absolute()


def safe_path(path):
    path = Path(path).expanduser().absolute()
    if ".." in path.parts:
        raise ValueError("parent traversal is not allowed")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"symlink path is not allowed: {part}")
    return path.resolve()


def read_json(path, default=None):
    path = safe_path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic(path, content):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".imozuru-", dir=path.parent)
    try:
        remaining = memoryview(content)
        while remaining:
            remaining = remaining[os.write(fd, remaining):]
        os.fsync(fd)
        os.close(fd)
        fd = None
        safe_path(path)
        os.replace(name, path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


@contextmanager
def locked(path):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            encoding="utf-8", timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.rstrip("\n")


def repository(path):
    repo = safe_path(path)
    if not repo.is_dir() or Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError("destination must be an existing local Git repository root")
    return repo


def git_preflight(config):
    repo = repository(config["repo"])
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch == "develop" or branch != config["branch"]:
        raise ValueError("target branch changed or is develop; configure an approved branch")
    if config["mode"] == "push" and "remote_fingerprint" in config:
        if digest(git(repo, "remote", "get-url", "--push", "--all", config["remote"]).encode("utf-8")) != config["remote_fingerprint"]:
            raise ValueError("remote push destination changed; configure explicit approval again")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("commit/push requires a completely clean target repository")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if Path(git(repo, "rev-parse", "--git-path", marker)).is_absolute():
            marker_path = Path(git(repo, "rev-parse", "--git-path", marker))
        else:
            marker_path = repo / git(repo, "rev-parse", "--git-path", marker)
        if marker_path.exists():
            raise ValueError("target has an active merge/rebase operation")
    return repo


def configuration():
    config = read_json(directory("CONFIG") / "autosave.json", {"version": 1, "mode": "unset"})
    if not isinstance(config, dict) or config.get("version") != 1 or config.get("mode") not in (*MODES, "unset"):
        raise ValueError("invalid autosave config; use configure to replace it explicitly")
    if config["mode"] in ("copy", "commit", "push"):
        if not isinstance(config.get("repo"), str) or not Path(config["repo"]).is_absolute():
            raise ValueError("invalid autosave destination")
        repository(config["repo"])
        if config["mode"] in ("commit", "push") and not isinstance(config.get("branch"), str):
            raise ValueError("invalid autosave branch")
        if config["mode"] == "push" and not isinstance(config.get("remote"), str):
            raise ValueError("invalid autosave remote")
    return config


def state():
    value = read_json(directory("STATE") / "autosave.json", {"version": 1, "topics": {}, "sessions": {}})
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("topics"), dict) or not isinstance(value.get("sessions"), dict):
        raise ValueError("invalid autosave state")
    return value


def save_state(value):
    write_json(directory("STATE") / "autosave.json", value)


def topic_path(path, config):
    topic = safe_path(path)
    if not topic.is_dir():
        raise ValueError("topic must be an existing directory")
    repo = Path(config["repo"])
    if topic == repo or topic in repo.parents or repo in topic.parents:
        raise ValueError("research topic and destination repository must not overlap")
    return topic


def register(config, value, path):
    if config["mode"] in ("unset", "off"):
        return {"mode": config["mode"], "registered": False}
    topic = topic_path(path, config)
    key = str(topic)
    slug = re.sub(r"[^a-z0-9-]+", "-", topic.name.lower()).strip("-")[:60] or "topic"
    identity = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    entry = value["topics"].setdefault(key, {"destination": f"research/imozuru/{slug}-{identity}", "targets": {}})
    if os.environ.get("CODEX_THREAD_ID"):
        bind(value, os.environ["CODEX_THREAD_ID"], key)
    save_state(value)
    return {"registered": True, "topic": key, "destination": str(Path(config["repo"]) / entry["destination"])}


def bind(value, session, topic):
    topics = value["sessions"].setdefault(session, [])
    if topic not in topics:
        topics.append(topic)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def sync(config, value, path):
    if config["mode"] in ("unset", "off"):
        return {"mode": config["mode"], "copied": 0, "committed": False, "pushed": False}
    repo = repository(config["repo"])
    with locked(Path(git(repo, "rev-parse", "--absolute-git-dir")) / "imozuru-autosave.lock"):
        return sync_locked(config, value, path)


def sync_locked(config, value, path):
    mode = config["mode"]
    if mode in ("unset", "off"):
        return {"mode": mode, "copied": 0, "committed": False, "pushed": False}
    topic = topic_path(path, config)
    entry = value["topics"].get(str(topic))
    if entry is None:
        raise ValueError("register this topic before syncing")
    repo = repository(config["repo"])
    destination = Path(entry["destination"])
    if destination.is_absolute() or ".." in destination.parts or destination.parts[:2] != ("research", "imozuru"):
        raise ValueError("invalid registered destination")
    dest = safe_path(repo / destination)
    target = entry["targets"].setdefault(str(repo), {})
    pending = entry.setdefault("pending", {})
    if mode in ("commit", "push") and str(repo) in pending:
        raise ValueError(f"unresolved previous Git save ({pending[str(repo)]}); inspect local files and Git state before explicit reconfiguration")
    files = [topic / name for name in ARTIFACTS]
    watch = topic / "watch"
    if watch.is_dir() and not watch.is_symlink():
        files.extend(sorted(watch.glob("*.md")))
    updates = {}
    for source in files:
        if source.is_symlink() or not source.is_file():
            continue
        safe_path(source)
        relative = source.relative_to(topic).as_posix()
        out = safe_path(dest / relative)
        content = source.read_bytes()
        expected = target.get(relative, [])
        if out.exists():
            if not out.is_file() or digest(out.read_bytes()) not in expected:
                raise ValueError(f"destination contains an unknown or user-edited file: {out}")
            if out.read_bytes() == content:
                continue
        updates[relative] = content
    result = {"mode": mode, "destination": str(dest), "copied": 0, "committed": False, "pushed": False}
    if not updates:
        return result
    if mode in ("commit", "push"):
        git_preflight(config)
        pending[str(repo)] = "copy/commit pending"
    for relative, content in updates.items():
        target[relative] = list(set(target.get(relative, []) + [digest(content)]))
    save_state(value)
    for relative, content in updates.items():
        atomic(dest / relative, content)
    result["copied"] = len(updates)
    if mode in ("commit", "push"):
        if git(repo, "symbolic-ref", "--quiet", "--short", "HEAD") != config["branch"]:
            raise ValueError("branch changed during copy; files retained without commit")
        owned = [(destination / name).as_posix() for name in updates]
        dirty = git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
        if any(line[3:] not in owned for line in dirty.split("\0") if line):
            raise ValueError("target changed during copy; files retained without staging")
        git(repo, "add", "--", *owned)
        git(repo, "commit", "--only", "-m", f"Save imozuru research: {topic.name}", "--", *owned)
        result["committed"] = True
        if mode == "push":
            pending[str(repo)] = "local commit created; push pending"
            save_state(value)
            git_preflight(config)
            git(repo, "push", "--", config["remote"], f"HEAD:refs/heads/{config['branch']}")
            result["pushed"] = True
        pending.pop(str(repo), None)
    for relative, content in updates.items():
        target[relative] = [digest(content)]
    save_state(value)
    return result


def command_tokens(payload):
    if payload.get("tool_name") not in ("Bash", "exec_command"):
        return []
    raw = payload.get("tool_input", {}).get("command", payload.get("tool_input", {}).get("cmd", ""))
    if not isinstance(raw, str) or any(char in raw for char in "\n;|&><`$"):
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return []
    if tokens[:1] == ["rtk"]:
        tokens = tokens[2:] if tokens[1:2] == ["proxy"] else tokens[1:]
    return tokens


def successful(payload):
    response = payload.get("tool_response", {})
    return isinstance(response, dict) and response.get("exit_code") == 0 and not response.get("isError")


def hook(config, value, payload):
    if config["mode"] == "off":
        return {}
    event = payload.get("hook_event_name")
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return {}
    if event == "Stop":
        if config["mode"] in ("unset", "off"):
            return {}
        for topic in value["sessions"].get(session, []):
            sync(config, value, topic)
        return {}
    if event != "PostToolUse":
        return {}
    tokens = command_tokens(payload)
    own = len(tokens) >= 3 and tokens[0] in ("python3", str(Path(sys.executable).resolve())) and tokens[1] == str(SCRIPT)
    if own and successful(payload) and tokens[2] == "register" and len(tokens) == 4:
        key = str(safe_path(tokens[3]))
        if key in value["topics"]:
            bind(value, session, key)
            save_state(value)
    read = False
    if payload.get("tool_name") == "Read":
        response = payload.get("tool_response", {})
        read = (payload.get("tool_input", {}).get("file_path") == str(SKILL)
                and isinstance(response, dict) and not response.get("isError")
                and any(name in response for name in ("content", "file", "text")))
    if successful(payload):
        read = read or tokens == ["cat", str(SKILL)] or (len(tokens) == 4 and tokens[:2] == ["sed", "-n"] and re.fullmatch(r"\d+,\d+p", tokens[2]) is not None and tokens[3] == str(SKILL))
    if config["mode"] == "unset" and (read or own and successful(payload) and tokens[2:] == ["status"]):
        notices = value.setdefault("notified_sessions", [])
        if session in notices:
            return {}
        notices.append(session)
        save_state(value)
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "imozuru 初回設定: hooks を使って調査記録を指定のローカル Git リポジトリへ自動保存するか、はい/いいえと保存先をユーザーに確認する。回答まで有効化しない。通常の「はい」は hooks の追加とコピー保存。/hooks の信頼前は動作中と報告しない。SKILL.md の初回設定に従う。"}}
    return {}


def install_hooks(path, apply):
    path = safe_path(path)
    data = read_json(path, {})
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError("invalid hooks config")
    hooks = data.setdefault("hooks", {})
    command = shlex.join([str(Path(sys.executable).resolve()), str(SCRIPT), "hook"])
    for event in ("PostToolUse", "Stop"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"invalid {event} hooks")
        owned = {"hooks": [{"type": "command", "command": command, "timeout": 30}]}
        if owned not in entries:
            entries.append(owned)
    if apply:
        write_json(path, data)
    return {"applied": apply, "path": str(path), "config": data}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    configure = sub.add_parser("configure")
    configure.add_argument("--mode", choices=MODES, required=True)
    configure.add_argument("--repo")
    configure.add_argument("--remote")
    for name in ("register", "sync"):
        sub.add_parser(name).add_argument("topic")
    sub.add_parser("hook")
    install = sub.add_parser("install-hooks")
    install.add_argument("path", help="explicit user or project hooks.json path")
    installation = install.add_mutually_exclusive_group()
    installation.add_argument("--apply", action="store_true", help="write the merged config; default prints a dry run")
    installation.add_argument("--dry-run", action="store_true", help="print the merged config without writing")
    args = parser.parse_args(argv)
    try:
        if args.command == "install-hooks":
            result = install_hooks(args.path, args.apply)
        else:
            with locked(directory("STATE") / "autosave.lock"):
                if args.command == "configure":
                    config = {"version": 1, "mode": args.mode}
                    if args.mode != "off":
                        if not args.repo:
                            raise ValueError("--repo is required to enable autosave")
                        repo = repository(args.repo)
                        config["repo"] = str(repo)
                        if args.mode in ("commit", "push"):
                            config["branch"] = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
                            git_preflight(config)
                        if args.mode == "push":
                            if not args.remote or args.remote.startswith("-") or args.remote not in git(repo, "remote").splitlines():
                                raise ValueError("--remote must name an explicitly approved existing remote")
                            config["remote"] = args.remote
                            config["remote_fingerprint"] = digest(git(repo, "remote", "get-url", "--push", "--all", args.remote).encode("utf-8"))
                        value = state()
                        for entry in value["topics"].values():
                            entry.get("pending", {}).pop(str(repo), None)
                        save_state(value)
                    write_json(directory("CONFIG") / "autosave.json", config)
                    result = config
                else:
                    config = configuration()
                    if args.command == "status":
                        result = {**config, "pending_git": {topic: entry.get("pending", {}).get(config.get("repo"))
                                                           for topic, entry in state()["topics"].items()
                                                           if config.get("repo") in entry.get("pending", {})}}
                    else:
                        value = state()
                        if args.command == "register":
                            result = register(config, value, args.topic)
                        elif args.command == "sync":
                            result = sync(config, value, args.topic)
                        else:
                            result = hook(config, value, json.load(sys.stdin))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as error:
        if args.command == "hook":
            print(json.dumps({"systemMessage": f"imozuru autosave failed: {error}. Local research files are retained; inspect before retrying."}))
            return 0
        print(f"imozuru autosave: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
