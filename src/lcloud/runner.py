from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import supervisor
from .api import (
    DEFAULT_BASE_URL,
    LambdaCloud,
    LambdaCloudError,
    LambdaCloudTransientError,
    available_regions,
    hourly_price,
    instance_type_name,
    iter_instance_types,
)
from .remote import Remote


@dataclass
class Upload:
    source: str
    destination: str


@dataclass
class Checkpoint:
    source: str
    destination: str
    interval_seconds: int = 300
    sync_timeout_seconds: int = 300
    exclude: list[str] = field(default_factory=lambda: ["*.tmp", "*.partial"])


@dataclass
class JobSpec:
    command: str
    instance_type: str
    ssh_key_name: str
    ssh_private_key: str
    timeout_seconds: int
    name: str = "lcloud-job"
    region: str | None = None
    filesystem: str | None = None
    workdir: str | None = None
    uploads: list[Upload] = field(default_factory=list)
    checkpoint: Checkpoint | None = None
    stop_grace_seconds: int = 60
    setup_allowance_seconds: int = 900
    job_user: str = "ubuntu"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JobSpec":
        data = dict(value)
        data["uploads"] = [Upload(**upload) for upload in data.get("uploads", [])]
        if data.get("checkpoint"):
            data["checkpoint"] = Checkpoint(**data["checkpoint"])
        return cls(**data)

    @classmethod
    def from_json(cls, path: str | os.PathLike[str]) -> "JobSpec":
        with open(path, encoding="utf-8") as file:
            return cls.from_dict(json.load(file))

    def validate(self) -> None:
        if not self.command.strip():
            raise ValueError("command must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.setup_allowance_seconds <= 0:
            raise ValueError("setup_allowance_seconds must be positive")
        if self.checkpoint and not self.filesystem:
            raise ValueError("checkpoint requires a filesystem")
        if self.checkpoint and Path(self.checkpoint.destination).is_absolute():
            raise ValueError("checkpoint.destination must be relative to the filesystem")
        if self.checkpoint and self.checkpoint.interval_seconds <= 0:
            raise ValueError("checkpoint.interval_seconds must be positive")


@dataclass
class SessionSpec:
    instance_type: str
    ssh_key_name: str
    ssh_private_key: str
    max_lifetime_seconds: int
    name: str = "lcloud-session"
    region: str | None = None
    filesystem: str | None = None
    uploads: list[Upload] = field(default_factory=list)
    setup_commands: list[str] = field(default_factory=list)
    job_user: str = "ubuntu"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SessionSpec":
        data = dict(value)
        data["uploads"] = [Upload(**upload) for upload in data.get("uploads", [])]
        return cls(**data)

    @classmethod
    def from_json(cls, path: str | os.PathLike[str]) -> "SessionSpec":
        with open(path, encoding="utf-8") as file:
            return cls.from_dict(json.load(file))

    def validate(self) -> None:
        if self.max_lifetime_seconds <= 0:
            raise ValueError("max_lifetime_seconds must be positive")
        for command in self.setup_commands:
            if not command.strip():
                raise ValueError("setup_commands must not contain empty commands")


@dataclass
class RunResult:
    run_id: str
    instance_id: str
    ip: str
    ssh_command: str
    hourly_price: float
    estimated_max_compute_cost: float


def estimate_compute_cost(hourly_rate: float, seconds: float) -> float:
    billable_minutes = math.ceil(seconds / 60)
    return hourly_rate * billable_minutes / 60


class Runner:
    def __init__(self, cloud: LambdaCloud | None = None) -> None:
        self.cloud = cloud or LambdaCloud()

    def resolve_offer(self, spec: JobSpec | SessionSpec) -> tuple[str, float]:
        required_region = spec.region
        if spec.filesystem:
            matching = [
                item
                for item in self.cloud.file_systems()
                if item.get("name") == spec.filesystem
            ]
            if not matching:
                raise LambdaCloudError(
                    f"Filesystem {spec.filesystem!r} is not available to this API key/workspace"
                )
            region_value = matching[0].get("region")
            filesystem_region = (
                region_value.get("name")
                if isinstance(region_value, dict)
                else region_value
            )
            if not filesystem_region:
                raise LambdaCloudError(
                    f"Lambda did not report a region for filesystem {spec.filesystem!r}"
                )
            filesystem_region = str(filesystem_region)
            if required_region and required_region != filesystem_region:
                raise LambdaCloudError(
                    f"Job region {required_region!r} does not match filesystem "
                    f"{spec.filesystem!r} region {filesystem_region!r}"
                )
            required_region = filesystem_region

        for item in iter_instance_types(self.cloud.instance_types()):
            if instance_type_name(item) != spec.instance_type:
                continue
            regions = available_regions(item)
            if required_region:
                if required_region not in regions:
                    raise LambdaCloudError(
                        f"{spec.instance_type} has no reported capacity in {required_region}; "
                        f"available regions: {regions or 'none'}"
                    )
                region = required_region
            else:
                if not regions:
                    raise LambdaCloudError(
                        f"{spec.instance_type} has no reported capacity"
                    )
                region = regions[0]
            price = hourly_price(item)
            if price is None:
                raise LambdaCloudError(
                    f"Lambda did not report a price for {spec.instance_type}"
                )
            return region, price
        raise LambdaCloudError(f"Unknown instance type: {spec.instance_type}")

    def storage_summary(self, spec: JobSpec | SessionSpec) -> str | None:
        if not spec.filesystem:
            return None
        configured_rate = os.environ.get("LCLOUD_STORAGE_RATE_PER_GIB_MONTH")
        rate: float | None = None
        if configured_rate:
            try:
                rate = float(configured_rate)
            except ValueError as error:
                raise ValueError(
                    "LCLOUD_STORAGE_RATE_PER_GIB_MONTH must be a number"
                ) from error
            if rate < 0:
                raise ValueError(
                    "LCLOUD_STORAGE_RATE_PER_GIB_MONTH must not be negative"
                )
        matching = [
            item
            for item in self.cloud.file_systems()
            if item.get("name") == spec.filesystem
        ]
        usage = matching[0].get("bytes_used") if matching else None
        if usage is None:
            usage_text = "current usage unavailable"
        else:
            gib = float(usage) / (1024**3)
            usage_text = f"current usage {gib:.1f} GiB"
            if rate is not None:
                usage_text += f" (${gib * rate:.2f}/month at current usage)"
        if rate is None:
            return (
                f"{spec.filesystem}: {usage_text}; "
                "rate unavailable from Lambda API"
            )
        return (
            f"{spec.filesystem}: {usage_text}; "
            f"configured rate ${rate:.4f}/GiB-month"
        )

    def check_ssh_key(self, ssh_key_name: str) -> None:
        ssh_key_names = {
            str(key.get("name"))
            for key in self.cloud.ssh_keys()
            if key.get("name") is not None
        }
        if ssh_key_name not in ssh_key_names:
            available = ", ".join(sorted(ssh_key_names)) or "none"
            raise LambdaCloudError(
                f"SSH key {ssh_key_name!r} is not available to this API key/workspace; "
                f"available Lambda SSH key names: {available}"
            )

    def run(
        self,
        spec: JobSpec,
        *,
        assume_yes: bool = False,
        follow_output: bool = True,
    ) -> RunResult:
        spec.validate()
        region, price = self.resolve_offer(spec)
        self.check_ssh_key(spec.ssh_key_name)
        estimated_seconds = spec.timeout_seconds + spec.setup_allowance_seconds
        estimated_cost = estimate_compute_cost(price, estimated_seconds)

        print(f"Instance: {spec.instance_type} in {region}")
        print(f"Rate: ${price:.2f}/hour")
        print(f"Job timeout: {spec.timeout_seconds / 3600:.2f} hours")
        print(f"Setup allowance: {spec.setup_allowance_seconds / 60:.0f} minutes")
        print(f"Maximum estimated compute charge: ${estimated_cost:.2f}")
        storage = self.storage_summary(spec)
        if storage:
            print(f"Persistent storage: {storage}")
        if not assume_yes:
            input("Press ENTER to launch, or Ctrl-C to cancel: ")

        run_id = uuid.uuid4().hex[:12]
        launched_at = int(time.time())
        hard_deadline = launched_at + estimated_seconds
        instance_id: str | None = None
        remote_started = False
        try:
            try:
                instance_id = self.cloud.launch(
                    region=region,
                    instance_type=spec.instance_type,
                    ssh_key_name=spec.ssh_key_name,
                    name=f"{spec.name}-{run_id}"[:64],
                    file_system_names=[spec.filesystem] if spec.filesystem else None,
                    tags={
                        "lcloud-managed": "true",
                        "lcloud-run-id": run_id,
                        "lcloud-launched-at": str(launched_at),
                        "lcloud-deadline": str(hard_deadline),
                    },
                )
            except LambdaCloudTransientError:
                print(
                    "Launch response was lost; checking for the tagged instance "
                    "instead of issuing a second launch."
                )
                instance_id = self._recover_ambiguous_launch(run_id)
                if instance_id is None:
                    raise LambdaCloudError(
                        "Launch outcome is still unknown and was not retried to avoid "
                        f"creating a duplicate. Run `lcloud list` and look for run {run_id}."
                    )
            print(f"Launched {instance_id}; waiting for API and SSH readiness...")
            instance = self.cloud.wait_until_active(instance_id)
            remote = Remote(instance["ip"], spec.ssh_private_key)
            remote.wait()
            print(f"SSH: {remote.ssh_command()}")
            setup_deadline = launched_at + spec.setup_allowance_seconds
            self._arm_emergency(
                remote,
                instance_id,
                max(1, setup_deadline - int(time.time())),
            )

            mount = None
            if spec.filesystem:
                mount = filesystem_mount(instance, spec.filesystem)
                remote.command(
                    "for attempt in $(seq 1 30); do "
                    f"if mountpoint -q -- {shell_quote(mount)} && "
                    f"sudo -H -u {shell_quote(spec.job_user)} -- "
                    f"test -w {shell_quote(mount)}; then exit 0; fi; "
                    "sleep 2; done; "
                    f"echo 'Filesystem is not mounted and writable: {shell_quote(mount)}' >&2; "
                    "exit 1"
                )

            for upload in spec.uploads:
                remote.command(f"mkdir -p -- {shell_quote(upload.destination)}")
                remote.rsync_to(upload.source, upload.destination)

            checkpoint_config = None
            if spec.checkpoint:
                assert mount is not None
                checkpoint_config = {
                    "source": spec.checkpoint.source,
                    "destination": f"{mount.rstrip('/')}/{spec.checkpoint.destination}",
                    "interval_seconds": spec.checkpoint.interval_seconds,
                    "sync_timeout_seconds": spec.checkpoint.sync_timeout_seconds,
                    "exclude": spec.checkpoint.exclude,
                }

            config = {
                "run_id": run_id,
                "instance_id": instance_id,
                "api_base_url": self.cloud.base_url,
                "command": spec.command,
                "workdir": spec.workdir,
                "timeout_seconds": spec.timeout_seconds,
                "hard_deadline_epoch": hard_deadline,
                "stop_grace_seconds": spec.stop_grace_seconds,
                "job_user": spec.job_user,
                "checkpoint": checkpoint_config,
                "status_destination": (
                    checkpoint_config["destination"]
                    if checkpoint_config
                    else f"{mount.rstrip('/')}/runs/{run_id}"
                    if mount
                    else None
                ),
            }
            self._install_supervisor(remote, config)
            remote_started = True
            print(f"Job {run_id} is running independently of this process.")
            result = RunResult(
                run_id=run_id,
                instance_id=instance_id,
                ip=instance["ip"],
                ssh_command=remote.ssh_command(),
                hourly_price=price,
                estimated_max_compute_cost=estimated_cost,
            )
            if follow_output:
                print("Following remote output; Ctrl-C only stops local following.")
                remote.stream(
                    "sudo journalctl --unit=lcloud-job.service "
                    "--follow --lines=all --output=cat"
                )
                print("Remote output stream ended.")
            return result
        finally:
            if instance_id is not None and not remote_started:
                print(f"Setup did not complete; terminating {instance_id}.")
                self.cloud.terminate([instance_id])

    def session(
        self,
        spec: SessionSpec,
        *,
        assume_yes: bool = False,
    ) -> RunResult:
        spec.validate()
        region, price = self.resolve_offer(spec)
        self.check_ssh_key(spec.ssh_key_name)
        estimated_cost = estimate_compute_cost(price, spec.max_lifetime_seconds)

        print(f"Instance: {spec.instance_type} in {region}")
        print(f"Rate: ${price:.2f}/hour")
        print(f"Maximum session lifetime: {spec.max_lifetime_seconds / 3600:.2f} hours")
        print(f"Maximum estimated compute charge: ${estimated_cost:.2f}")
        storage = self.storage_summary(spec)
        if storage:
            print(f"Persistent storage: {storage}")
        if not assume_yes:
            input("Press ENTER to launch, or Ctrl-C to cancel: ")

        run_id = uuid.uuid4().hex[:12]
        launched_at = int(time.time())
        hard_deadline = launched_at + spec.max_lifetime_seconds
        instance_id: str | None = None
        session_ready = False
        try:
            try:
                instance_id = self.cloud.launch(
                    region=region,
                    instance_type=spec.instance_type,
                    ssh_key_name=spec.ssh_key_name,
                    name=f"{spec.name}-{run_id}"[:64],
                    file_system_names=[spec.filesystem] if spec.filesystem else None,
                    tags={
                        "lcloud-managed": "true",
                        "lcloud-kind": "session",
                        "lcloud-run-id": run_id,
                        "lcloud-launched-at": str(launched_at),
                        "lcloud-deadline": str(hard_deadline),
                    },
                )
            except LambdaCloudTransientError:
                print(
                    "Launch response was lost; checking for the tagged instance "
                    "instead of issuing a second launch."
                )
                instance_id = self._recover_ambiguous_launch(run_id)
                if instance_id is None:
                    raise LambdaCloudError(
                        "Launch outcome is still unknown and was not retried to avoid "
                        f"creating a duplicate. Run `lcloud list` and look for run {run_id}."
                    )
            print(f"Launched {instance_id}; waiting for API and SSH readiness...")
            instance = self.cloud.wait_until_active(instance_id)
            remote = Remote(instance["ip"], spec.ssh_private_key)
            remote.wait()
            print(f"SSH: {remote.ssh_command()}")
            self._arm_emergency(
                remote,
                instance_id,
                max(1, hard_deadline - int(time.time())),
            )

            if spec.filesystem:
                mount = filesystem_mount(instance, spec.filesystem)
                remote.command(
                    "for attempt in $(seq 1 30); do "
                    f"if mountpoint -q -- {shell_quote(mount)} && "
                    f"sudo -H -u {shell_quote(spec.job_user)} -- "
                    f"test -w {shell_quote(mount)}; then exit 0; fi; "
                    "sleep 2; done; "
                    f"echo 'Filesystem is not mounted and writable: {shell_quote(mount)}' >&2; "
                    "exit 1"
                )
                print(f"Filesystem mounted at {mount}")

            for upload in spec.uploads:
                remote.command(f"mkdir -p -- {shell_quote(upload.destination)}")
                remote.rsync_to(upload.source, upload.destination)

            for command in spec.setup_commands:
                remote.command(command)

            session_ready = True
            print(f"Session {run_id} is ready.")
            print(f"Instance ID: {instance_id}")
            print(f"SSH: {remote.ssh_command()}")
            print(
                "The instance will keep running until you terminate it or the "
                "max-lifetime guard fires."
            )
            return RunResult(
                run_id=run_id,
                instance_id=instance_id,
                ip=instance["ip"],
                ssh_command=remote.ssh_command(),
                hourly_price=price,
                estimated_max_compute_cost=estimated_cost,
            )
        finally:
            if instance_id is not None and not session_ready:
                print(f"Session setup did not complete; terminating {instance_id}.")
                self.cloud.terminate([instance_id])

    def _recover_ambiguous_launch(
        self,
        run_id: str,
        *,
        timeout: float = 90,
        interval: float = 5,
    ) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matching = []
            for instance in self.cloud.instances():
                tags = {
                    str(tag.get("key")): str(tag.get("value"))
                    for tag in instance.get("tags", [])
                    if tag.get("key") is not None
                }
                if tags.get("lcloud-run-id") == run_id:
                    matching.append(str(instance["id"]))
            if len(matching) == 1:
                return matching[0]
            if len(matching) > 1:
                raise LambdaCloudError(
                    f"Multiple instances unexpectedly have lcloud run ID {run_id}: {matching}"
                )
            time.sleep(interval)
        return None

    def _arm_emergency(
        self, remote: Remote, instance_id: str, setup_seconds_remaining: int
    ) -> None:
        remote.command(
            "sudo install -d -m 700 /etc/lcloud /var/lib/lcloud /usr/local/lib/lcloud"
        )
        remote.command(
            "sudo sh -c 'umask 077; cat > /etc/lcloud/api-key'",
            stdin=(self.cloud.api_key + "\n").encode(),
        )
        remote.command(
            "sudo sh -c 'umask 077; cat > /etc/lcloud/instance-id'",
            stdin=(instance_id + "\n").encode(),
        )
        remote.command(
            "sudo sh -c 'cat > /usr/local/bin/lcloud-emergency-terminate; chmod 700 /usr/local/bin/lcloud-emergency-terminate'",
            stdin=emergency_script(self.cloud.base_url).encode(),
        )
        remote.command(
            "sudo sh -c 'cat > /etc/systemd/system/lcloud-emergency-terminate.service'",
            stdin=EMERGENCY_UNIT.encode(),
        )
        remote.command(
            "sudo sh -c 'cat > /etc/systemd/system/lcloud-reboot-guard.service'",
            stdin=REBOOT_UNIT.encode(),
        )
        remote.command(
            "sudo systemctl daemon-reload && "
            "sudo systemctl enable lcloud-reboot-guard.service && "
            "sudo touch /var/lib/lcloud/armed && "
            "(sudo systemctl is-active --quiet lcloud-setup-guard.timer || "
            f"sudo systemd-run --unit=lcloud-setup-guard "
            f"--on-active={setup_seconds_remaining}s "
            "/usr/local/bin/lcloud-emergency-terminate)"
        )

    def _install_supervisor(self, remote: Remote, config: dict[str, Any]) -> None:
        source = Path(supervisor.__file__).read_bytes()
        remote.command(
            "sudo sh -c 'umask 077; cat > /usr/local/lib/lcloud/supervisor.py'",
            stdin=source,
        )
        remote.command(
            "sudo sh -c 'umask 077; cat > /etc/lcloud/job.json'",
            stdin=(json.dumps(config, indent=2) + "\n").encode(),
        )
        remote.command(
            "sudo sh -c 'cat > /etc/systemd/system/lcloud-job.service'",
            stdin=JOB_UNIT.encode(),
        )
        remote.command(
            "sudo systemctl daemon-reload && "
            "sudo systemctl start lcloud-job.service && "
            "sudo systemctl stop lcloud-setup-guard.timer"
        )


def filesystem_mount(instance: dict[str, Any], filesystem_name: str) -> str:
    mounts = instance.get("file_system_mounts") or []
    if len(mounts) == 1 and mounts[0].get("mount_point"):
        return str(mounts[0]["mount_point"])
    return f"/lambda/nfs/{filesystem_name}"


def shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def emergency_script(api_base_url: str = DEFAULT_BASE_URL) -> str:
    return f"""#!/bin/sh
set -eu
key=$(cat /etc/lcloud/api-key)
instance_id=$(cat /etc/lcloud/instance-id)
exec curl --fail --silent --show-error --retry 120 --retry-delay 15 --retry-all-errors \\
  --connect-timeout 10 --max-time 30 --request POST \\
  --header "Authorization: Bearer $key" --header "Content-Type: application/json" \\
  --data "{{\\"instance_ids\\":[\\"$instance_id\\"]}}" \\
  {shell_quote(api_base_url + '/instance-operations/terminate')}
"""


JOB_UNIT = """[Unit]
Description=lcloud experiment supervisor
After=network-online.target
Wants=network-online.target
OnFailure=lcloud-emergency-terminate.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/lib/lcloud/supervisor.py
Restart=no
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
"""


EMERGENCY_UNIT = """[Unit]
Description=Immediately terminate a failed lcloud instance
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/lcloud-emergency-terminate
Restart=on-failure
RestartSec=15
"""


REBOOT_UNIT = """[Unit]
Description=Terminate an lcloud instance after an unexpected reboot
After=network-online.target
Wants=network-online.target
ConditionPathExists=/var/lib/lcloud/armed

[Service]
Type=oneshot
ExecStart=/usr/local/bin/lcloud-emergency-terminate

[Install]
WantedBy=multi-user.target
"""
