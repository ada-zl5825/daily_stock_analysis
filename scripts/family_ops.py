#!/usr/bin/env python3
"""Small, local-only Docker lifecycle manager for a family stock deployment.

Python 3.9+ and the standard library are sufficient. Commands print JSON without
credential values. All mutable state lives under the explicitly initialized root.
Docker and Git are invoked without a shell; builds use committed Git archives.
"""

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


REPO = Path(__file__).resolve().parents[1]
ENVIRONMENTS = ("production", "staging")
IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
NOTIFY_PREFIXES = (
    "DINGTALK_", "WECHAT_", "WEIXIN_", "FEISHU_", "TELEGRAM_", "EMAIL_",
    "PUSHPLUS_", "SERVERCHAN_", "SERVER_CHAN_", "BARK_", "DISCORD_",
    "SLACK_", "ASTRBOT_", "CUSTOM_WEBHOOK_", "NOTIFICATION_", "PUSHDEER_",
    "GOTIFY_", "NTFY_", "WECOM_", "QQ_", "PUSHOVER_", "SERVERCHAN3_",
)
INITIAL_ENV = """ADMIN_AUTH_ENABLED=true
STOCK_LIST=
SCHEDULE_ENABLED=false
SCHEDULE_RUN_IMMEDIATELY=false
RUN_IMMEDIATELY=false
AGENT_EVENT_MONITOR_ENABLED=false
BOT_ENABLED=false
NEWS_INTEL_AUTO_FETCH_ENABLED=false
STOCK_INDEX_REMOTE_UPDATE_ENABLED=false
SCREENING_ENABLED=false
SEARXNG_PUBLIC_INSTANCES_ENABLED=false
MAX_WORKERS=1
"""


class OpsError(RuntimeError):
    """An actionable error whose message contains no command output or secrets."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def identifier():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def write_private(path, content):
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp), str(path))
        path.chmod(0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_json(path, obj):
    write_private(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OpsError("Unreadable JSON: " + path.name) from exc


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_links(path):
    """Never traverse symlinks/junctions when copying or deleting deployment data."""
    for item in (path,) + tuple(path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise OpsError("Symlink/junction paths are not supported")
    if path.is_dir():
        for base, dirs, files in os.walk(str(path), followlinks=False):
            for name in dirs + files:
                child = Path(base) / name
                if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                    raise OpsError("Symlink/junction inside deployment data")
                if not child.is_dir() and not child.is_file():
                    raise OpsError("Only regular files and directories may be backed up")


def copy_private(source, target):
    reject_links(source)
    if source.is_dir():
        shutil.copytree(str(source), str(target))
        for base, dirs, files in os.walk(str(target)):
            Path(base).chmod(0o700)
            for name in files:
                (Path(base) / name).chmod(0o600)
    else:
        shutil.copyfile(str(source), str(target))
        target.chmod(0o600)


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if KEY_RE.fullmatch(key):
            values[key] = value.strip().strip("\"'")
    return values


def configured(value):
    return bool(value and value.lower() not in {"xxx", "xxxx", "sk-xxx", "changeme"}
                and not value.lower().startswith(("your_", "your-")))


def has_model_key(values):
    return any(configured(value) for key, value in values.items()
               if (key.startswith("LLM_") or key.startswith(
                   ("OPENAI_", "GEMINI_", "DEEPSEEK_", "ANTHROPIC_")))
               and key.endswith(("API_KEY", "API_KEYS")))


def staging_env(text):
    """Remove delivery credentials, rather than assume a nonexistent global switch."""
    lines = []
    for line in text.splitlines():
        key = line.partition("=")[0].strip().removeprefix("export ").strip()
        if key.startswith(NOTIFY_PREFIXES) or key in {
            "SCHEDULE_ENABLED", "RUN_IMMEDIATELY", "BOT_ENABLED",
            "SINGLE_STOCK_NOTIFY", "MERGE_EMAIL_NOTIFICATION", "DSA_TIMEOUT_PARTIAL_NOTIFY",
            "SCHEDULE_RUN_IMMEDIATELY", "AGENT_EVENT_MONITOR_ENABLED",
            "NEWS_INTEL_AUTO_FETCH_ENABLED", "SCREENING_ENABLED",
        }:
            continue
        lines.append(line)
    lines.extend(["SCHEDULE_ENABLED=false", "RUN_IMMEDIATELY=false", "BOT_ENABLED=false",
                  "SCHEDULE_RUN_IMMEDIATELY=false", "AGENT_EVENT_MONITOR_ENABLED=false",
                  "NEWS_INTEL_AUTO_FETCH_ENABLED=false", "SCREENING_ENABLED=false",
                  "DINGTALK_STREAM_ENABLED=false", "FEISHU_STREAM_ENABLED=false",
                  "SINGLE_STOCK_NOTIFY=false", "DSA_TIMEOUT_PARTIAL_NOTIFY=false"])
    return "\n".join(lines) + "\n"


class FamilyOps:
    def __init__(self, root, repo=REPO):
        raw = Path(root).expanduser().absolute()
        reject_links(raw)
        self.root = raw.resolve()
        self.repo = Path(repo).resolve()
        if self.root in (Path(self.root.anchor), Path.home().resolve(), self.repo):
            raise OpsError("Use a dedicated deployment directory, not home, repository, or filesystem root")

    def inside(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        reject_links(path)
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise OpsError("Path must stay inside the deployment root") from exc
        if resolved == self.root:
            raise OpsError("Operation cannot target the deployment root itself")
        return resolved

    @contextlib.contextmanager
    def lock(self):
        if self.root.exists() and not (self.root / "family-ops.json").exists():
            if any(p.name != ".maintenance.lock" for p in self.root.iterdir()):
                raise OpsError("Refusing to change an uninitialized nonempty directory")
        private_dir(self.root)
        path = self.inside(".maintenance.lock")
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise OpsError("maintenance_lock_busy; retry after the current operation") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise OpsError("maintenance_lock_busy; retry after the current operation") from exc
            yield
        finally:
            os.close(fd)  # OS releases the lock, including after process failure.

    def config(self):
        config = read_json(self.inside("family-ops.json"))
        if config.get("schema") != 1:
            raise OpsError("Deployment root is not initialized or has an unsupported schema")
        return config

    def env_dir(self, environment):
        if environment not in ENVIRONMENTS:
            raise OpsError("Unknown environment")
        return self.inside(Path("environments") / environment)

    def manifest(self, environment):
        self.config()
        result = read_json(self.env_dir(environment) / "deployment.json")
        if result.get("environment") != environment:
            raise OpsError("Environment manifest mismatch")
        if result.get("image") and not IMAGE_RE.fullmatch(result["image"]):
            raise OpsError("Deployment image must be an immutable sha256 ID")
        return result

    def run(self, args, env=None, cwd=None, stdin=None, timeout=None):
        try:
            result = subprocess.run(args, env=env, cwd=cwd or self.repo, stdin=stdin,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise OpsError("{} command timed out".format(args[0])) from exc
        except OSError as exc:
            raise OpsError("Cannot execute " + args[0]) from exc
        if result.returncode:
            # subprocess output may contain provider configuration: never echo it.
            raise OpsError("{} command failed (exit {})".format(args[0], result.returncode))
        return result.stdout.decode("utf-8", errors="replace").strip()

    def compose(self, environment, *args, timeout=None):
        config = self.config()
        deployment = self.manifest(environment)
        base = self.env_dir(environment)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FAMILY_", "COMPOSE_"))}
        env.update({
            "FAMILY_IMAGE": deployment["image"] or "family-stock:unbuilt",
            "FAMILY_PORT": str(config[environment]["port"]),
            "FAMILY_SCHEDULE_ENABLED": str(deployment["schedule_enabled"]).lower(),
            "FAMILY_UID": str(config["uid"]), "FAMILY_GID": str(config["gid"]),
        })
        for key in ("data", "logs", "reports", "strategies"):
            env["FAMILY_" + key.upper() + "_DIR"] = str(self.inside(base / key))
        return self.run(["docker", "compose", "--project-name", config[environment]["project"],
                         "-f", str(base / "compose.yml"), "--profile", "scheduler"] + list(args),
                        env=env, cwd=base, timeout=timeout)

    def init(self, production_port=8000, staging_port=8001,
             production_project="family-stock", staging_project="family-stock-staging",
             enable_scheduler=False):
        if (self.root / "family-ops.json").exists():
            raise OpsError("Root already initialized; existing data was left intact")
        if any(p.name != ".maintenance.lock" for p in self.root.iterdir()):
            raise OpsError("Initialization requires an empty dedicated directory")
        if not all(1 <= p <= 65535 for p in (production_port, staging_port)):
            raise OpsError("Ports must be between 1 and 65535")
        if production_port == staging_port or production_project == staging_project:
            raise OpsError("Production and staging must have different ports and project names")
        if not all(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", p)
                   for p in (production_project, staging_project)):
            raise OpsError("Invalid Compose project name")
        if enable_scheduler:
            raise OpsError("Initialize first, configure a model key, then use schedule --enable")
        template = self.repo / "docker" / "family-compose.yml"
        if not template.is_file():
            raise OpsError("docker/family-compose.yml is missing")
        config = {"schema": 1, "created_at": now(),
                  "uid": os.getuid() if hasattr(os, "getuid") else 1000,
                  "gid": os.getgid() if hasattr(os, "getgid") else 1000,
                  "production": {"port": production_port, "project": production_project},
                  "staging": {"port": staging_port, "project": staging_project}}
        for name in ("backups", "builds", "recoveries", "operations"):
            private_dir(self.inside(name))
        for environment in ENVIRONMENTS:
            base = self.env_dir(environment)
            for name in ("data/home", "logs", "reports", "strategies"):
                private_dir(base / name)
            copy_private(template, base / "compose.yml")
            write_private(base / "data/runtime.env", INITIAL_ENV)
            write_json(base / "deployment.json", {"schema": 1, "environment": environment,
                       "image": None, "commit": None, "schedule_enabled": False,
                       "public_ready": False, "updated_at": now()})
        write_json(self.inside("family-ops.json"), config)
        return {"initialized": True, "root": str(self.root), "scheduler_enabled": False}

    def running(self, environment):
        return self.compose(environment, "ps", "--services", "--status", "running").splitlines()

    @contextlib.contextmanager
    def stopped(self, environment):
        previous = self.running(environment)
        allow_uninitialized = not self.manifest(environment).get("public_ready", False)
        try:
            if previous:
                self.compose(environment, "stop", "--timeout", "60", *previous)
            if self.running(environment):
                raise OpsError("Could not stop every writer in the Compose project")
            yield previous
        finally:
            if previous:
                self.compose(environment, "start", *previous)
                if "server" in previous:
                    # Compose start only confirms the process launch. Do not release
                    # maintenance or report success before HTTP/auth/DB are usable.
                    self.smoke(environment, allow_uninitialized=allow_uninitialized)

    def image(self, image):
        result = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        if not IMAGE_RE.fullmatch(result):
            raise OpsError("Image inspection did not return one immutable image ID")
        return result

    def build(self, ref):
        if not ref or ref.startswith("-"):
            raise OpsError("Invalid Git ref")
        if self.run(["git", "status", "--porcelain"]):
            raise OpsError("Build requires a clean committed checkout")
        commit = self.run(["git", "rev-parse", "--verify", ref + "^{commit}"])
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise OpsError("Git ref did not resolve to a commit")
        build_dir = self.inside(Path("builds") / identifier())
        private_dir(build_dir)
        archive = build_dir / "context.tar"
        iid = build_dir / "image.id"
        try:
            self.run(["git", "archive", "--format=tar", "--output=" + str(archive), commit])
            archive.chmod(0o600)
            version = self.run(["git", "describe", "--tags", "--always", commit])
            config = self.config()
            with archive.open("rb") as stream:
                self.run(["docker", "build", "--pull", "--iidfile", str(iid),
                          "--label", "org.opencontainers.image.revision=" + commit,
                          "--build-arg", "DSA_WEB_VERSION=" + version,
                          "--build-arg", "DSA_WEB_REVISION=" + commit,
                          "--build-arg", "DSA_UID=" + str(config["uid"]),
                          "--build-arg", "DSA_GID=" + str(config["gid"]),
                          "-t", "family-stock:" + commit[:12], "-f", "docker/Dockerfile", "-"],
                         stdin=stream)
            image = self.image(iid.read_text(encoding="utf-8").strip())
            record = {"image": image, "commit": commit, "ref": ref, "version": version,
                      "built_at": now(), "context": "git archive",
                      "uid": config["uid"], "gid": config["gid"]}
            write_json(build_dir / "build.json", record)
            return record
        finally:
            if archive.exists():
                archive.unlink()

    def build_record(self, image):
        for path in sorted(self.inside("builds").glob("*/build.json"), reverse=True):
            record = read_json(self.inside(path))
            if record.get("image") == image:
                return record
        raise OpsError("Image has no local committed build record; use build --ref first")

    def snapshot(self, environment, reason):
        """Caller holds the maintenance lock and has stopped all writers."""
        base = self.env_dir(environment)
        effective = self.compose(environment, "config", "--format", "json")
        target = self.inside(Path("backups") / (identifier() + "-" + environment))
        pending = target.with_name(".pending-" + target.name)
        private_dir(pending)
        try:
            for directory in ("data", "reports", "strategies"):
                copy_private(base / directory, pending / directory)
            for name in ("deployment.json", "compose.yml"):
                copy_private(base / name, pending / name)
            write_private(pending / "effective-compose.json", effective + "\n")
            hashes = {p.relative_to(pending).as_posix(): sha256(p)
                      for p in pending.rglob("*") if p.is_file()}
            write_json(pending / "backup.json", {"schema": 1, "environment": environment,
                       "created_at": now(), "reason": reason, "sha256": hashes})
            pending.rename(target)
        except BaseException:
            if pending.exists():
                shutil.rmtree(str(self.inside(pending)))
            raise
        self.verify_backup(target)
        return target

    def verify_backup(self, backup):
        path = Path(backup)
        if not path.is_absolute():
            path = self.root / "backups" / path
        path = self.inside(path)
        if path.parent != self.inside("backups") or path.name.startswith("."):
            raise OpsError("Use a backup directory directly inside root/backups")
        metadata = read_json(path / "backup.json")
        if metadata.get("schema") != 1 or metadata.get("environment") not in ENVIRONMENTS:
            raise OpsError("Invalid backup manifest")
        expected = metadata.get("sha256", {})
        required = {"deployment.json", "compose.yml", "effective-compose.json", "data/runtime.env"}
        actual = {p.relative_to(path).as_posix() for p in path.rglob("*")
                  if p.is_file() and p != path / "backup.json"}
        if not all((path / name).is_dir() for name in ("data", "reports", "strategies")):
            raise OpsError("Backup is missing a required data directory")
        if not required.issubset(expected) or actual != set(expected):
            raise OpsError("Backup file inventory mismatch")
        for name, digest in expected.items():
            candidate = self.inside(path / name)
            if not candidate.is_relative_to(path) or sha256(candidate) != digest:
                raise OpsError("Backup SHA256 verification failed")
        return path, metadata

    def prune(self, environment, retention_days):
        if retention_days < 1:
            raise OpsError("Retention must be at least one day")
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
        protected = self.manifest(environment).get("previous_backup")
        for path in self.inside("backups").iterdir():
            if not path.is_dir() or path.name.startswith(".") or path.name == protected:
                continue
            try:
                checked, meta = self.verify_backup(path)
            except OpsError:
                continue  # Unverified directories are retained for manual inspection.
            if meta["environment"] == environment and dt.datetime.fromisoformat(meta["created_at"]) < cutoff:
                shutil.rmtree(str(checked))

    def backup(self, environment, retention_days=14):
        if retention_days < 1:
            raise OpsError("Retention must be at least one day")
        with self.stopped(environment):
            path = self.snapshot(environment, "scheduled-or-manual")
        self.prune(environment, retention_days)
        return {"backup": path.name, "verified": True, "retention_days": retention_days}

    def request(self, port, route):
        # Explicitly bypass host HTTP proxies for these loopback-only probes.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open("http://127.0.0.1:{}{}".format(port, route), timeout=5) as response:
                return response.status, json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except (OSError, ValueError):
            return 0, {}

    def smoke(self, environment, allow_uninitialized=False, timeout=90):
        port = self.config()[environment]["port"]
        deadline = time.monotonic() + timeout
        while True:
            code, health = self.request(port, "/api/health")
            if code == 200 and health.get("status") == "ok":
                auth_code, auth = self.request(port, "/api/v1/auth/status")
                protected_code, _ = self.request(port, "/api/v1/portfolio/accounts")
                ready = auth.get("passwordSet") is True
                if (auth_code == 200 and auth.get("authEnabled") is True
                        and protected_code == 401 and (ready or allow_uninitialized)):
                    database = self.db_smoke(environment)
                    return {"health": True, "auth_enabled": True, "password_set": ready,
                            "anonymous_denied": True, "public_ready": ready, "database": database}
            if time.monotonic() >= deadline:
                raise OpsError("Health/auth smoke failed; inspect local container logs")
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def db_smoke(self, environment):
        """Exercise real application migrations and portfolio reads in the container."""
        code = (
            "import contextlib,io,json,logging,os,sqlite3\n"
            "logging.disable(logging.CRITICAL)\n"
            "with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):\n"
            " from src.storage import DatabaseManager\n"
            " from src.services.portfolio_service import PortfolioService\n"
            " DatabaseManager.get_instance()\n"
            " count=len(PortfolioService().list_accounts())\n"
            " with contextlib.closing(sqlite3.connect(os.environ['DATABASE_PATH'],timeout=10)) as db:\n"
            "  assert db.execute('PRAGMA quick_check').fetchall()==[('ok',)], 'SQLite integrity check failed'\n"
            "print(json.dumps({'ok':True,'account_count':count}))\n"
        )
        result = self.compose(environment, "exec", "-T", "server", "python", "-c", code, timeout=90)
        try:
            parsed = json.loads(result)
        except ValueError as exc:
            raise OpsError("Database smoke did not produce a valid result") from exc
        if parsed.get("ok") is not True or not isinstance(parsed.get("account_count"), int):
            raise OpsError("Database smoke failed")
        return {"ok": True, "account_count": parsed["account_count"]}

    def up(self, environment, services=None):
        self.compose(environment, "up", "-d", "--no-build", "--pull", "never", *(services or ["server"]))

    def stop_all(self, environment):
        services = self.running(environment)
        if services:
            self.compose(environment, "stop", "--timeout", "60", *services)
        if self.running(environment):
            raise OpsError("Compose writers are still running")

    def replace_directory(self, environment, source, directory):
        if directory not in ("data", "reports", "strategies"):
            raise OpsError("Unsupported deployment directory")
        source = self.inside(source)
        base = self.env_dir(environment)
        target = self.inside(base / directory)
        pending = self.inside(base / (".restore-" + uuid.uuid4().hex))
        copy_private(source, pending)
        recovery = self.inside(Path("recoveries") / (identifier() + "-" + environment))
        private_dir(recovery)
        target.rename(recovery / directory)
        try:
            pending.rename(target)
        except BaseException:
            (recovery / directory).rename(target)
            raise
        return recovery.name

    def restore_payload(self, environment, backup):
        path, meta = self.verify_backup(backup)
        if meta["environment"] != environment:
            raise OpsError("Backup belongs to another environment")
        base = self.env_dir(environment)
        recoveries = {directory: self.replace_directory(environment, path / directory, directory)
                      for directory in ("data", "reports", "strategies")}
        for name in ("deployment.json", "compose.yml"):
            copy_private(path / name, base / name)
        return recoveries

    def deploy(self, environment, image):
        image = self.image(image)
        record = self.build_record(image)
        old = self.manifest(environment)
        previous = self.running(environment)
        self.stop_all(environment)
        try:
            snapshot = self.snapshot(environment, "pre-deploy")
        except BaseException:
            if previous:
                self.compose(environment, "start", *previous)
            raise
        new = dict(old, image=image, commit=record["commit"], version=record["version"],
                   previous_backup=snapshot.name, updated_at=now(), public_ready=False)
        try:
            write_json(self.env_dir(environment) / "deployment.json", new)
            self.up(environment)
            smoke = self.smoke(environment, allow_uninitialized=old["image"] is None)
            new["public_ready"] = smoke["public_ready"]
            write_json(self.env_dir(environment) / "deployment.json", new)
        except BaseException as exc:
            self.stop_all(environment)
            self.restore_payload(environment, snapshot)
            if previous and old["image"]:
                self.up(environment, previous)
                self.smoke(environment, allow_uninitialized=not old.get("public_ready", False))
            raise OpsError("Candidate failed; previous image, config and data restored from " + snapshot.name) from exc
        self.prune(environment, 14)
        return {"deployed": environment, "image": image, "commit": record["commit"],
                "backup": snapshot.name, "smoke": smoke}

    def stage(self, image=None):
        production = self.manifest("production")
        candidate = image or production["image"]
        if not candidate:
            raise OpsError("Build/deploy production or provide a candidate image first")
        candidate = self.image(candidate)
        record = self.build_record(candidate)
        with self.stopped("production"):
            snapshot = self.snapshot("production", "staging-source")
        previous = self.running("staging")
        old = self.manifest("staging")
        self.stop_all("staging")
        try:
            before = self.snapshot("staging", "pre-stage")
        except BaseException:
            if previous:
                self.compose("staging", "start", *previous)
            raise
        try:
            for directory in ("data", "reports", "strategies"):
                self.replace_directory("staging", snapshot / directory, directory)
            session_secret = self.env_dir("staging") / "data/.session_secret"
            if session_secret.exists():
                session_secret.unlink()  # Same password is allowed; production sessions are not.
            env_file = self.env_dir("staging") / "data/runtime.env"
            write_private(env_file, staging_env(env_file.read_text(encoding="utf-8")))
            manifest = dict(old, image=candidate, commit=record["commit"], version=record["version"],
                            schedule_enabled=False, source_backup=snapshot.name,
                            previous_backup=before.name, updated_at=now(), public_ready=False)
            write_json(self.env_dir("staging") / "deployment.json", manifest)
            self.up("staging")
            smoke = self.smoke("staging", allow_uninitialized=not production.get("public_ready", False))
            manifest["public_ready"] = smoke["public_ready"]
            write_json(self.env_dir("staging") / "deployment.json", manifest)
        except BaseException:
            self.stop_all("staging")
            self.restore_payload("staging", before)
            if previous and old["image"]:
                self.up("staging", previous)
            raise
        return {"staged": candidate, "source_backup": snapshot.name, "isolated_data": True, "smoke": smoke}

    def promote(self):
        candidate = self.manifest("staging")
        if not candidate["image"] or not candidate.get("source_backup"):
            raise OpsError("Stage and validate a candidate before promotion")
        self.smoke("staging")
        # Deploy only the image; staging data/config never replace production.
        return self.deploy("production", candidate["image"])

    def restore(self, environment, backup=None):
        old = self.manifest(environment)
        backup = backup or old.get("previous_backup")
        if not backup:
            raise OpsError("No rollback backup recorded")
        path, meta = self.verify_backup(backup)
        if meta["environment"] != environment:
            raise OpsError("Backup belongs to another environment")
        restored = read_json(path / "deployment.json")
        if restored.get("image"):
            self.image(restored["image"])  # Fail before changing anything if image is missing.
        previous = self.running(environment)
        self.stop_all(environment)
        try:
            before = self.snapshot(environment, "pre-restore")
        except BaseException:
            if previous:
                self.compose(environment, "start", *previous)
            raise
        try:
            recovery = self.restore_payload(environment, path)
            if restored.get("image"):
                self.up(environment)
                self.smoke(environment, allow_uninitialized=not restored.get("public_ready", False))
        except BaseException as exc:
            self.stop_all(environment)
            self.restore_payload(environment, before)
            if previous and old["image"]:
                self.up(environment, previous)
            raise OpsError("Restore failed; current data preserved and pre-restore snapshot reinstated") from exc
        restored["previous_backup"] = before.name
        restored["updated_at"] = now()
        write_json(self.env_dir(environment) / "deployment.json", restored)
        return {"restored": path.name, "pre_restore_backup": before.name, "recovery": recovery}

    def schedule(self, environment, enabled):
        manifest = self.manifest(environment)
        if enabled and environment == "staging":
            raise OpsError("Staging schedules are disabled")
        env_file = self.env_dir(environment) / "data/runtime.env"
        if enabled and not has_model_key(read_env(env_file)):
            raise OpsError("Configure a model API key before enabling the scheduler")
        old = dict(manifest)
        manifest["schedule_enabled"] = enabled
        manifest["updated_at"] = now()
        write_json(self.env_dir(environment) / "deployment.json", manifest)
        try:
            if manifest["image"]:
                # Analyzer is never a second automatic scheduling owner.
                if "analyzer" in self.running(environment):
                    self.compose(environment, "stop", "--timeout", "60", "analyzer")
                self.up(environment)
                self.smoke(environment, allow_uninitialized=not old.get("public_ready", False))
        except BaseException:
            write_json(self.env_dir(environment) / "deployment.json", old)
            if old["image"]:
                self.up(environment)
            raise
        return {"environment": environment, "schedule_enabled": enabled}

    def ensure_running(self, environment):
        manifest = self.manifest(environment)
        if not manifest["image"]:
            return {"skipped": "no deployed image"}
        self.image(manifest["image"])
        self.up(environment)
        return {"environment": environment, "image": manifest["image"], "started": True}

    def status(self, environment):
        manifest = self.manifest(environment)
        port = self.config()[environment]["port"]
        health_code, health = self.request(port, "/api/health")
        auth_code, auth = self.request(port, "/api/v1/auth/status")
        protected_code, _ = self.request(port, "/api/v1/portfolio/accounts")
        values = read_env(self.env_dir(environment) / "data/runtime.env")
        keys = {key: configured(value) for key, value in values.items()
                if any(term in key for term in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        freshness = {}
        db = self.inside(self.env_dir(environment) / "data/stock_analysis.db")
        if db.exists():
            try:
                with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table, column in (("stock_daily", "date"), ("analysis_history", "created_at")):
                        if table in tables:
                            freshness[table] = connection.execute(
                                "SELECT MAX({}) FROM {}".format(column, table)).fetchone()[0]
            except sqlite3.Error:
                freshness["error"] = "database_read_failed"
        try:
            running = self.running(environment)
            docker_error = None
        except OpsError:
            running, docker_error = [], "docker_unavailable"
        return {"environment": environment, "deployment": manifest, "running_services": running,
                "docker_error": docker_error, "health": health_code == 200 and health.get("status") == "ok",
                "auth_enabled": auth_code == 200 and auth.get("authEnabled") is True,
                "password_set": auth_code == 200 and auth.get("passwordSet") is True,
                "anonymous_denied": protected_code == 401, "configured_keys": keys,
                "model_key_configured": has_model_key(values), "data_latest": freshness,
                "checked_at": now()}

    def check_updates(self):
        self.config()
        self.run(["git", "fetch", "--no-tags", "upstream",
                  "+refs/tags/*:refs/family-upstream/tags/*"])
        refs = self.run(["git", "for-each-ref", "--sort=-version:refname", "--count=20",
                         "--format=%(refname:strip=3)", "refs/family-upstream/tags/"]).splitlines()
        result = {"checked_at": now(), "upstream_tags": refs, "automatic_upgrade": False,
                  "production_commit": self.manifest("production").get("commit")}
        write_json(self.inside("operations/update-check.json"), result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Dedicated deployment directory")
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--production-port", type=int, default=8000)
    init.add_argument("--staging-port", type=int, default=8001)
    init.add_argument("--production-project", default="family-stock")
    init.add_argument("--staging-project", default="family-stock-staging")
    init.add_argument("--enable-scheduler", action="store_true")
    sub.add_parser("status")
    build = sub.add_parser("build")
    build.add_argument("--ref", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--retention-days", type=int, default=14)
    stage = sub.add_parser("stage")
    stage.add_argument("--image")
    deploy = sub.add_parser("deploy")
    deploy.add_argument("--image", required=True)
    sub.add_parser("promote")
    for command in ("restore", "rollback"):
        restore = sub.add_parser(command)
        restore.add_argument("backup", nargs="?" if command == "rollback" else None)
    schedule = sub.add_parser("schedule")
    group = schedule.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    sub.add_parser("ensure-running")
    sub.add_parser("check-updates")
    args = parser.parse_args(argv)
    try:
        ops = FamilyOps(args.root, args.repo)
        if args.command == "status":
            result = ops.status(args.environment)
        else:
            with ops.lock():
                if args.command == "init":
                    result = ops.init(args.production_port, args.staging_port,
                                      args.production_project, args.staging_project, args.enable_scheduler)
                elif args.command == "build":
                    ops.config()
                    result = ops.build(args.ref)
                elif args.command == "backup":
                    result = ops.backup(args.environment, args.retention_days)
                elif args.command == "stage":
                    result = ops.stage(args.image)
                elif args.command == "deploy":
                    result = ops.deploy(args.environment, args.image)
                elif args.command == "promote":
                    result = ops.promote()
                elif args.command in ("restore", "rollback"):
                    result = ops.restore(args.environment, args.backup)
                elif args.command == "schedule":
                    result = ops.schedule(args.environment, args.enable)
                elif args.command == "ensure-running":
                    result = ops.ensure_running(args.environment)
                else:
                    result = ops.check_updates()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OpsError, OSError, ValueError) as exc:
        # Unexpected OS errors may include path names, but never file contents.
        message = str(exc) if isinstance(exc, OpsError) else type(exc).__name__
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
