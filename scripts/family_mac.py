#!/usr/bin/env python3
"""Install and run user-level macOS maintenance jobs for a family deployment.

No credentials or portfolio data are passed through launchd arguments or logs.
The container runtime is installed separately; this script never upgrades it.
"""

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time


JOBS = {
    "ensure": {"RunAtLoad": True, "StartInterval": 120},
    "monitor": {"RunAtLoad": True, "StartInterval": 900},
    "backup": {"StartCalendarInterval": {"Hour": 1, "Minute": 45}},
    "updates": {"StartCalendarInterval": {"Weekday": 0, "Hour": 9, "Minute": 0}},
}


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def install(args):
    if sys.platform != "darwin":
        raise SystemExit("launchd installation must run on the target Mac")
    root = args.root.resolve()
    if not (root / "environments" / "production" / "deployment.json").is_file():
        raise SystemExit("Initialize the deployment with family_ops.py before installing jobs")
    source_script = Path(__file__).resolve()
    ops_script = source_script.with_name("family_ops.py")
    if not ops_script.is_file():
        raise SystemExit("family_ops.py is missing beside family_mac.py")
    runtime_bin = args.runtime_bin.expanduser().resolve()
    environment = {
        "PATH": str(runtime_bin) + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "DOCKER_CONFIG": str(args.docker_config.expanduser().resolve()),
        "DOCKER_CONTEXT": args.docker_context,
        "PYTHONUNBUFFERED": "1",
    }
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    (root / "maintenance").mkdir(mode=0o700, exist_ok=True)
    config = {"root": str(root), "source": str(source_script.parent.parent), "environment": environment}
    atomic_json(root / "maintenance" / "mac-jobs.json", config)
    installed = []
    domain = "gui/" + str(os.getuid())
    for name, calendar in JOBS.items():
        label = args.label_prefix + "." + name
        path = launch_agents / (label + ".plist")
        if path.exists():
            old = plistlib.loads(path.read_bytes())
            old_args = old.get("ProgramArguments", [])
            if str(source_script) not in old_args or str(root) not in old_args:
                raise SystemExit("Refusing to replace unrelated launch agent: " + str(path))
        job = {
            "Label": label,
            "ProgramArguments": [sys.executable, str(source_script), "--root", str(root), "run", name],
            "EnvironmentVariables": environment,
            "WorkingDirectory": str(root),
            "ProcessType": "Background",
            "LowPriorityIO": True,
            **calendar,
        }
        path.write_bytes(plistlib.dumps(job))
        path.chmod(0o600)
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True, check=False)
        result = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
        if result.returncode:
            raise SystemExit("launchctl bootstrap failed for " + label + ": " + result.stderr.strip())
        installed.append(label)
    print(json.dumps({"installed": installed, "timezone": "Mac local timezone for backup/update jobs"}))


def run_job(args):
    root = args.root.resolve()
    maintenance = root / "maintenance"
    config = json.loads((maintenance / "mac-jobs.json").read_text(encoding="utf-8"))
    environment = dict(os.environ)
    environment.update(config["environment"])
    commands = {
        "ensure": ["ensure-running"],
        "monitor": ["status"],
        "backup": ["backup", "--retention-days", "14"],
        "updates": ["check-updates"],
    }
    command = [sys.executable, str(Path(config["source"]) / "scripts" / "family_ops.py"), "--root", str(root)]
    command.extend(commands[args.job])
    started = time.time()
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True)
        # family_ops emits sanitized JSON, never raw .env contents. Bound log size
        # further and retain only the last result instead of an unbounded daemon log.
        output = result.stdout[-16000:]
        try:
            payload = json.loads(output)
        except ValueError:
            payload = {"message": "Command did not return JSON; inspect manually without exporting secrets"}
        record = {"job": args.job, "timestamp": int(started), "duration_seconds": round(time.time() - started, 2),
                  "exit_code": result.returncode, "result": payload}
        if args.job == "monitor" and result.returncode == 0:
            record["healthy"] = all(payload.get(key) is True for key in
                                    ("health", "auth_enabled", "password_set", "anonymous_denied"))
            record["configuration_pending"] = payload.get("model_key_configured") is not True
            if not record["healthy"]:
                record["exit_code"] = 1
    except OSError:
        record = {"job": args.job, "timestamp": int(started), "exit_code": 1, "error": "maintenance_start_failed"}
    atomic_json(maintenance / ("last-" + args.job + ".json"), record)
    if args.job == "monitor":
        atomic_json(maintenance / "health.json", record)
    print(json.dumps({"job": args.job, "exit_code": record["exit_code"]}))
    return record["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("install")
    setup.add_argument("--runtime-bin", type=Path, required=True)
    setup.add_argument("--docker-config", type=Path, required=True)
    setup.add_argument("--docker-context", required=True)
    setup.add_argument("--label-prefix", default="com.theo.family-stock")
    job = sub.add_parser("run")
    job.add_argument("job", choices=tuple(JOBS))
    args = parser.parse_args()
    if args.command == "install":
        install(args)
        return 0
    return run_job(args)


if __name__ == "__main__":
    sys.exit(main())
