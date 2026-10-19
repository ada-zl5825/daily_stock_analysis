"""Offline lifecycle acceptance tests; real files/SQLite/Git, fake Docker boundary."""

import contextlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "family_ops", Path(__file__).resolve().parents[1] / "scripts/family_ops.py")
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)
IMAGE_A = "sha256:" + "a" * 64
IMAGE_B = "sha256:" + "b" * 64


class FakeDockerOps(ops.FamilyOps):
    """Only Docker/network are substituted; backup/restore filesystem paths are real."""
    def __init__(self, *args):
        super().__init__(*args)
        self.services = {"production": [], "staging": []}
        self.calls = []
        self.fail_image = None

    def compose(self, environment, *args):
        self.calls.append((environment, args))
        if args[0] == "ps":
            return "\n".join(self.services[environment])
        if args[0] == "config":
            return json.dumps({"services": {"server": {"image": self.manifest(environment)["image"]}}})
        if args[0] == "stop":
            stopped = args[3:] if args[1:3] == ("--timeout", "60") else args[1:]
            self.services[environment] = [s for s in self.services[environment] if s not in stopped]
        elif args[0] == "start":
            self.services[environment] = list(dict.fromkeys(self.services[environment] + list(args[1:])))
        elif args[0] == "up":
            names = list(args[5:])
            self.services[environment] = names
        return ""

    def image(self, image):
        if image not in (IMAGE_A, IMAGE_B):
            raise ops.OpsError("missing image")
        return image

    def smoke(self, environment, allow_uninitialized=False, timeout=90):
        if self.manifest(environment)["image"] == self.fail_image:
            (self.env_dir(environment) / "data/candidate-write.txt").write_text("migration")
            raise ops.OpsError("candidate smoke failed")
        return {"health": True, "auth_enabled": True, "password_set": True,
                "anonymous_denied": True, "public_ready": True}

    def request(self, port, route):
        if route.endswith("auth/status"):
            return 200, {"authEnabled": True, "passwordSet": True}
        if route.endswith("accounts"):
            return 401, {}
        return 200, {"status": "ok"}

    def db_smoke(self, environment):
        return {"ok": True, "account_count": 0}


class FamilyOpsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        (self.repo / "docker").mkdir(parents=True)
        (self.repo / "docker/family-compose.yml").write_text("services: {}\n")
        self.root = self.base / "deployment"
        self.ops = FakeDockerOps(self.root, self.repo)
        with self.ops.lock():
            self.ops.init()
        for suffix, image in (("a", IMAGE_A), ("b", IMAGE_B)):
            ops.write_json(self.root / "builds" / suffix / "build.json",
                           {"image": image, "commit": suffix * 40, "version": "v1"})

    def production(self):
        manifest = self.ops.manifest("production")
        manifest.update(image=IMAGE_A, public_ready=True, commit="a" * 40)
        ops.write_json(self.root / "environments/production/deployment.json", manifest)
        self.ops.services["production"] = ["server", "analyzer"]
        (self.root / "environments/production/data/portfolio.txt").write_text("original holdings")
        (self.root / "environments/production/reports/daily.md").write_text("original report")
        (self.root / "environments/production/strategies/family.md").write_text("original strategy")

    def test_initialization_is_private_isolated_and_not_overwriting(self):
        production = self.root / "environments/production/data/runtime.env"
        staging = self.root / "environments/staging/data/runtime.env"
        self.assertNotEqual(production, staging)
        self.assertIn("ADMIN_AUTH_ENABLED=true", production.read_text())
        self.assertIn("NEWS_INTEL_AUTO_FETCH_ENABLED=false", production.read_text())
        self.assertTrue((production.parent / "home").is_dir())
        if os.name != "nt":
            self.assertEqual(production.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ops.OpsError):
            self.ops.init()

    def test_all_writers_stopped_and_original_services_resumed(self):
        self.production()
        result = self.ops.backup("production")
        path, meta = self.ops.verify_backup(result["backup"])
        self.assertIn("data/runtime.env", meta["sha256"])
        self.assertIn("effective-compose.json", meta["sha256"])
        self.assertIn("reports/daily.md", meta["sha256"])
        self.assertIn("strategies/family.md", meta["sha256"])
        self.assertEqual((path / "data/portfolio.txt").read_text(), "original holdings")
        self.assertEqual(self.ops.services["production"], ["server", "analyzer"])
        self.assertIn(("production", ("stop", "--timeout", "60", "server", "analyzer")), self.ops.calls)

    def test_backup_failure_resumes_previous_services(self):
        self.production()
        with patch.object(ops, "copy_private", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.ops.backup("production")
        self.assertEqual(self.ops.services["production"], ["server", "analyzer"])
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_hash_tampering_refused_before_stopping_services(self):
        self.production()
        result = self.ops.backup("production")
        backup = self.root / "backups" / result["backup"]
        (backup / "data/runtime.env").write_text("changed")
        self.ops.calls.clear()
        with self.assertRaises(ops.OpsError):
            self.ops.restore("production", result["backup"])
        self.assertFalse(any(args[0] == "stop" for _, args in self.ops.calls))

    def test_paths_outside_root_are_rejected(self):
        for path in (self.base, self.root / "../outside", "../outside"):
            with self.assertRaises(ops.OpsError):
                self.ops.inside(path)
        with self.assertRaises(ops.OpsError):
            self.ops.verify_backup("../../repo")

    def test_symlink_backup_is_rejected(self):
        target = self.base / "outside.txt"
        target.write_text("private")
        link = self.root / "environments/production/data/link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Host does not allow symlink creation")
        with self.assertRaises(ops.OpsError):
            self.ops.backup("production")

    def test_deploy_failure_restores_image_database_config_and_running_services(self):
        self.production()
        self.ops.fail_image = IMAGE_B
        with self.assertRaisesRegex(ops.OpsError, "previous image, config and data restored"):
            self.ops.deploy("production", IMAGE_B)
        self.assertEqual(self.ops.manifest("production")["image"], IMAGE_A)
        self.assertEqual((self.root / "environments/production/data/portfolio.txt").read_text(), "original holdings")
        self.assertFalse((self.root / "environments/production/data/candidate-write.txt").exists())
        self.assertTrue(list((self.root / "recoveries").glob("*/data/candidate-write.txt")))
        self.assertEqual(self.ops.services["production"], ["server", "analyzer"])

    def test_successful_deploy_starts_only_server_and_records_rollback(self):
        self.production()
        result = self.ops.deploy("production", IMAGE_B)
        self.assertEqual(self.ops.services["production"], ["server"])
        self.assertEqual(self.ops.manifest("production")["image"], IMAGE_B)
        self.assertEqual(self.ops.manifest("production")["previous_backup"], result["backup"])

    def test_stage_copies_snapshot_disables_schedule_delivery_and_keeps_production(self):
        self.production()
        env = self.root / "environments/production/data/runtime.env"
        env.write_text(env.read_text() + "DEEPSEEK_API_KEY=model-secret\nTELEGRAM_BOT_TOKEN=delivery-secret\n"
                       "EMAIL_GROUP_1=recipient@example.com\nSCHEDULE_ENABLED=true\n"
                       "PUSHOVER_USER_KEY=pushover-secret\nSERVERCHAN3_SENDKEY=serverchan-secret\n")
        original = env.read_bytes()
        production_secret = env.parent / ".session_secret"
        production_secret.write_bytes(b"p" * 32)
        (env.parent / ".admin_password_hash").write_text("password-hash")
        result = self.ops.stage(IMAGE_B)
        staged = self.root / "environments/staging/data/runtime.env"
        self.assertNotIn("delivery-secret", staged.read_text())
        self.assertNotIn("recipient@example.com", staged.read_text())
        self.assertNotIn("pushover-secret", staged.read_text())
        self.assertNotIn("serverchan-secret", staged.read_text())
        self.assertIn("DEEPSEEK_API_KEY=model-secret", staged.read_text())
        self.assertIn("SCHEDULE_ENABLED=false", staged.read_text())
        self.assertEqual(env.read_bytes(), original)
        self.assertEqual(production_secret.read_bytes(), b"p" * 32)
        self.assertFalse((staged.parent / ".session_secret").exists())
        self.assertEqual((staged.parent / ".admin_password_hash").read_text(), "password-hash")
        self.assertFalse(self.ops.manifest("staging")["schedule_enabled"])
        self.assertEqual(self.ops.services["production"], ["server", "analyzer"])
        self.assertEqual(self.ops.services["staging"], ["server"])
        self.assertTrue(result["isolated_data"])
        self.assertEqual((self.root / "environments/staging/reports/daily.md").read_text(), "original report")
        self.assertEqual((self.root / "environments/staging/strategies/family.md").read_text(), "original strategy")
        (self.root / "environments/staging/data/portfolio.txt").write_text("test-only")
        self.assertEqual((self.root / "environments/production/data/portfolio.txt").read_text(), "original holdings")

    def test_promote_only_promotes_image_not_staging_data(self):
        self.production()
        self.ops.stage(IMAGE_B)
        (self.root / "environments/staging/data/portfolio.txt").write_text("test-only")
        self.ops.promote()
        self.assertEqual(self.ops.manifest("production")["image"], IMAGE_B)
        self.assertEqual((self.root / "environments/production/data/portfolio.txt").read_text(), "original holdings")

    def test_restore_preserves_current_data_before_replacing(self):
        self.production()
        first = self.ops.backup("production")["backup"]
        data = self.root / "environments/production/data/portfolio.txt"
        data.write_text("new holdings")
        result = self.ops.restore("production", first)
        self.assertEqual(data.read_text(), "original holdings")
        before, _ = self.ops.verify_backup(result["pre_restore_backup"])
        self.assertEqual((before / "data/portfolio.txt").read_text(), "new holdings")
        self.assertEqual((self.root / "recoveries" / result["recovery"]["data"] / "data/portfolio.txt").read_text(), "new holdings")

    def test_restore_rejects_cross_environment_backup(self):
        backup = self.ops.backup("production")["backup"]
        with self.assertRaisesRegex(ops.OpsError, "another environment"):
            self.ops.restore("staging", backup)

    def test_schedule_refuses_missing_key_and_staging(self):
        with self.assertRaisesRegex(ops.OpsError, "model API key"):
            self.ops.schedule("production", True)
        with self.assertRaises(ops.OpsError):
            self.ops.schedule("staging", True)
        env = self.root / "environments/production/data/runtime.env"
        env.write_text(env.read_text() + "LLM_DEEPSEEK_API_KEY=real-key\n")
        self.ops.schedule("production", True)
        self.assertTrue(self.ops.manifest("production")["schedule_enabled"])
        self.ops.schedule("production", False)
        self.assertFalse(self.ops.manifest("production")["schedule_enabled"])

    def test_status_only_shows_key_booleans_and_real_database_dates(self):
        env = self.root / "environments/production/data/runtime.env"
        env.write_text(env.read_text() + "DEEPSEEK_API_KEY=never-print-this\n")
        with contextlib.closing(sqlite3.connect(str(env.parent / "stock_analysis.db"))) as db:
            db.execute("CREATE TABLE stock_daily(date TEXT)")
            db.execute("INSERT INTO stock_daily VALUES ('2026-10-16')")
            db.commit()
        result = self.ops.status("production")
        self.assertNotIn("never-print-this", json.dumps(result))
        self.assertIs(result["configured_keys"]["DEEPSEEK_API_KEY"], True)
        self.assertEqual(result["data_latest"]["stock_daily"], "2026-10-16")

    def test_maintenance_lock_excludes_second_writer_and_releases_after_error(self):
        other = FakeDockerOps(self.root, self.repo)
        with self.ops.lock():
            with self.assertRaisesRegex(ops.OpsError, "maintenance_lock_busy"):
                with other.lock():
                    self.fail("second writer acquired lock")
        with other.lock():
            pass

    def test_ensure_running_never_builds_or_pulls(self):
        self.assertIn("skipped", self.ops.ensure_running("production"))
        self.production()
        self.ops.ensure_running("production")
        args = self.ops.calls[-1][1]
        self.assertEqual(args, ("up", "-d", "--no-build", "--pull", "never", "server"))

    def test_update_check_uses_private_refs_without_merge_or_checkout(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return "v3.34.0\nv3.33.0" if "for-each-ref" in args else ""

        self.ops.run = run
        result = self.ops.check_updates()
        self.assertFalse(result["automatic_upgrade"])
        self.assertIn("+refs/tags/*:refs/family-upstream/tags/*", calls[0])
        self.assertTrue((self.root / "operations/update-check.json").is_file())
        self.assertFalse(any("merge" in call or "checkout" in call for call in calls))

    def test_smoke_rejects_anonymous_access_even_with_healthy_http(self):
        def request(port, route):
            if route.endswith("auth/status"):
                return 200, {"authEnabled": True, "passwordSet": True}
            return 200, {"status": "ok"}

        self.ops.request = request
        with self.assertRaisesRegex(ops.OpsError, "Health/auth smoke failed"):
            ops.FamilyOps.smoke(self.ops, "production", timeout=0)

    def test_database_failure_triggers_deploy_rollback(self):
        self.production()
        self.ops.smoke = lambda environment, **kwargs: ops.FamilyOps.smoke(self.ops, environment, timeout=0, **kwargs)

        def db_smoke(environment):
            if self.ops.manifest(environment)["image"] == IMAGE_B:
                (self.ops.env_dir(environment) / "data/portfolio.txt").write_text("bad migration")
                raise ops.OpsError("migration failed")
            return {"ok": True, "account_count": 0}

        self.ops.db_smoke = db_smoke
        with self.assertRaisesRegex(ops.OpsError, "previous image, config and data restored"):
            self.ops.deploy("production", IMAGE_B)
        self.assertEqual(self.ops.manifest("production")["image"], IMAGE_A)
        self.assertEqual((self.ops.env_dir("production") / "data/portfolio.txt").read_text(), "original holdings")

    def test_nested_backup_json_is_included_in_hash_inventory(self):
        nested = self.ops.env_dir("production") / "data/backup.json"
        nested.write_text('{"user_document":true}')
        result = self.ops.backup("production")
        _, manifest = self.ops.verify_backup(result["backup"])
        self.assertIn("data/backup.json", manifest["sha256"])

    def test_smoke_allows_unset_password_only_explicit_bootstrap(self):
        original = self.ops.request
        self.ops.request = lambda port, route: (
            (200, {"authEnabled": True, "passwordSet": False})
            if route.endswith("auth/status") else original(port, route))
        with self.assertRaises(ops.OpsError):
            ops.FamilyOps.smoke(self.ops, "production", timeout=0)
        result = ops.FamilyOps.smoke(self.ops, "production", allow_uninitialized=True, timeout=0)
        self.assertFalse(result["public_ready"])

    def test_real_http_health_auth_probe(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("accounts"):
                    self.send_response(401)
                    self.end_headers()
                    return
                body = ({"authEnabled": True, "passwordSet": True}
                        if self.path.endswith("auth/status") else {"status": "ok"})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = self.ops.config()
        config["production"]["port"] = server.server_address[1]
        ops.write_json(self.root / "family-ops.json", config)
        self.ops.request = lambda port, route: ops.FamilyOps.request(self.ops, port, route)
        result = ops.FamilyOps.smoke(self.ops, "production", timeout=0)
        self.assertTrue(result["anonymous_denied"])

    @unittest.skipUnless(shutil.which("git"), "Git is required for build provenance test")
    def test_build_uses_exact_clean_committed_archive_and_records_image(self):
        def git(*args):
            return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]
                                           + list(args), cwd=self.repo, stderr=subprocess.DEVNULL).decode().strip()

        git("init")
        (self.repo / "version.txt").write_text("first")
        git("add", ".")
        git("commit", "-m", "first")
        first = git("rev-parse", "HEAD")
        (self.repo / "version.txt").write_text("second")
        git("add", ".")
        git("commit", "-m", "second")
        original_run = self.ops.run
        docker_calls = []

        def run(args, **kwargs):
            if args[0] != "docker":
                return original_run(args, **kwargs)
            docker_calls.append(args)
            with tarfile.open(fileobj=kwargs["stdin"], mode="r:") as archive:
                self.assertEqual(archive.extractfile("version.txt").read(), b"first")
                self.assertNotIn(".git/config", archive.getnames())
            Path(args[args.index("--iidfile") + 1]).write_text(IMAGE_B)
            return ""

        self.ops.run = run
        result = self.ops.build(first)
        self.assertEqual(result["commit"], first)
        self.assertEqual(result["image"], IMAGE_B)
        self.assertIn("DSA_UID=" + str(self.ops.config()["uid"]), docker_calls[0])
        self.assertFalse(list((self.root / "builds").glob("*/context.tar")))
        (self.repo / "version.txt").write_text("dirty")
        with self.assertRaisesRegex(ops.OpsError, "clean committed"):
            self.ops.build(first)

    def test_cli_error_does_not_echo_secret_command_output(self):
        real = ops.FamilyOps(self.root, self.repo)
        fake = subprocess.CompletedProcess(["docker"], 1, b"", b"SECRET=do-not-print")
        with patch.object(ops.subprocess, "run", return_value=fake):
            with self.assertRaises(ops.OpsError) as raised:
                real.run(["docker", "compose", "config"])
        self.assertNotIn("do-not-print", str(raised.exception))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = ops.main(["--root", str(self.root), "--repo", str(self.repo), "ensure-running"])
        self.assertEqual(result, 0)
        self.assertIn("skipped", output.getvalue())


if __name__ == "__main__":
    unittest.main()
