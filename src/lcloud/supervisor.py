from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


CONFIG_PATH = "/etc/lcloud/job.json"
KEY_PATH = "/etc/lcloud/api-key"
ARMED_PATH = "/var/lib/lcloud/armed"
STATUS_PATH = "/var/lib/lcloud/status.json"


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as file:
        return json.load(file)


def sync_checkpoints(config: dict[str, Any]) -> None:
    checkpoint = config.get("checkpoint")
    if not checkpoint:
        return
    source = checkpoint["source"].rstrip("/") + "/"
    destination = checkpoint["destination"].rstrip("/") + "/"
    Path(destination).mkdir(parents=True, exist_ok=True)
    command = ["rsync", "-a", "--delay-updates"]
    for pattern in checkpoint.get("exclude", ["*.tmp", "*.partial"]):
        command.extend(["--exclude", pattern])
    command.extend([source, destination])
    subprocess.run(
        command,
        check=True,
        timeout=float(checkpoint.get("sync_timeout_seconds", 300)),
    )


def write_status(config: dict[str, Any], status: dict[str, Any]) -> None:
    destination = config.get("status_destination")
    if not destination:
        return
    status_path = Path(STATUS_PATH)
    temporary = status_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, status_path)
    Path(destination).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rsync", "-a", "--delay-updates", STATUS_PATH, f"{destination}/lcloud-status.json"],
        check=True,
        timeout=30,
    )


def terminate(config: dict[str, Any]) -> None:
    key = Path(KEY_PATH).read_text().strip()
    body = json.dumps({"instance_ids": [config["instance_id"]]}).encode()
    request = urllib.request.Request(
        f"{config['api_base_url']}/instance-operations/terminate",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30):
        pass


def stop_process_group(process: subprocess.Popen[Any], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run() -> int:
    config = load_config()
    started_at = time.time()
    deadline = min(
        float(config["hard_deadline_epoch"]),
        started_at + float(config["timeout_seconds"]),
    )
    process = subprocess.Popen(
        [
            "sudo",
            "-H",
            "-u",
            config.get("job_user", "ubuntu"),
            "--",
            "bash",
            "-lc",
            config["command"],
        ],
        cwd=config.get("workdir") or None,
        start_new_session=True,
    )
    interval = float(config.get("checkpoint", {}).get("interval_seconds", 300))
    next_sync = time.monotonic() + interval
    timed_out = False

    while process.poll() is None:
        if time.time() >= deadline:
            timed_out = True
            stop_process_group(process, float(config.get("stop_grace_seconds", 60)))
            break
        if config.get("checkpoint") and time.monotonic() >= next_sync:
            try:
                sync_checkpoints(config)
            except Exception as error:
                print(f"periodic checkpoint sync failed: {error}", file=sys.stderr)
            next_sync = time.monotonic() + interval
        time.sleep(1)

    status = {
        "run_id": config["run_id"],
        "instance_id": config["instance_id"],
        "started_at_epoch": started_at,
        "finished_at_epoch": time.time(),
        "return_code": process.returncode,
        "timed_out": timed_out,
        "final_sync_error": None,
    }
    try:
        sync_checkpoints(config)
    except Exception as error:
        status["final_sync_error"] = str(error)
        print(f"final checkpoint sync failed: {error}", file=sys.stderr)

    try:
        write_status(config, status)
    except Exception as error:
        print(f"persisting final status failed: {error}", file=sys.stderr)

    print(f"[lcloud] final status: {json.dumps(status, sort_keys=True)}", flush=True)

    terminate(config)
    Path(ARMED_PATH).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
