from __future__ import annotations

import argparse
import subprocess
import shlex
import sys
import time
from typing import Any

from .api import (
    LambdaCloud,
    available_regions,
    hourly_price,
    instance_type_name,
    iter_instance_types,
)
from .remote import Remote
from .runner import JobSpec, Runner, SessionSpec, estimate_compute_cost


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lcloud")
    subparsers = parser.add_subparsers(dest="command", required=True)

    offers = subparsers.add_parser("offers", help="show instance prices and capacity")
    offers.add_argument("--available", action="store_true", help="only show available types")

    subparsers.add_parser("ssh-keys", help="list Lambda SSH key names and IDs")

    run = subparsers.add_parser("run", help="launch a job from a JSON specification")
    run.add_argument("spec", help="path to job JSON")
    run.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    run.add_argument("--max-cost", type=float, help="refuse if compute estimate exceeds this")
    run.add_argument(
        "--detach",
        action="store_true",
        help="return after remote startup instead of following job output",
    )

    session = subparsers.add_parser("session", help="launch an interactive setup session")
    session.add_argument("spec", help="path to session JSON")
    session.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    session.add_argument("--max-cost", type=float, help="refuse if compute estimate exceeds this")

    listing = subparsers.add_parser("list", help="list running instances")
    listing.add_argument("--key", help="SSH private key, to include pasteable SSH commands")

    terminate = subparsers.add_parser("terminate", help="terminate instances")
    terminate.add_argument("instance_ids", nargs="+")
    terminate.add_argument("--yes", action="store_true")

    reconcile = subparsers.add_parser("reconcile", help="terminate expired lcloud instances")
    reconcile.add_argument("--yes", action="store_true")

    ssh = subparsers.add_parser("ssh", help="print a pasteable SSH command")
    ssh.add_argument("target", help="instance ID, IP address, or hostname")
    ssh.add_argument("--key", required=True)

    execute = subparsers.add_parser("exec", help="run one command on an instance over SSH")
    execute.add_argument("target", help="instance ID, IP address, or hostname")
    execute.add_argument("--key", required=True)
    execute.add_argument("--user", default="ubuntu")
    execute.add_argument("--cwd", help="remote working directory")
    execute.add_argument("remote_command", nargs=argparse.REMAINDER)

    push = subparsers.add_parser("push", help="rsync local files to an instance")
    push.add_argument("target", help="instance ID, IP address, or hostname")
    push.add_argument("source")
    push.add_argument("destination")
    push.add_argument("--key", required=True)
    push.add_argument("--user", default="ubuntu")

    pull = subparsers.add_parser("pull", help="rsync files from an instance")
    pull.add_argument("target", help="instance ID, IP address, or hostname")
    pull.add_argument("source")
    pull.add_argument("destination")
    pull.add_argument("--key", required=True)
    pull.add_argument("--user", default="ubuntu")
    pull.add_argument(
        "--every",
        type=float,
        help="repeat the pull every N seconds until interrupted",
    )
    pull.add_argument(
        "--delete",
        action="store_true",
        help="delete local files that no longer exist on the remote source",
    )
    return parser


def tag_map(instance: dict[str, Any]) -> dict[str, str]:
    return {
        str(tag.get("key")): str(tag.get("value"))
        for tag in instance.get("tags", [])
        if tag.get("key") is not None
    }


def command_offers(cloud: LambdaCloud, *, only_available: bool) -> None:
    rows = []
    for item in iter_instance_types(cloud.instance_types()):
        regions = available_regions(item)
        if only_available and not regions:
            continue
        price = hourly_price(item)
        rows.append((instance_type_name(item), price, regions))
    for name, price, regions in sorted(rows):
        price_text = f"${price:.2f}/hour" if price is not None else "price unavailable"
        capacity = ", ".join(regions) if regions else "no capacity"
        print(f"{name:28} {price_text:16} {capacity}")


def command_list(cloud: LambdaCloud, key: str | None) -> None:
    prices = {
        instance_type_name(item): hourly_price(item)
        for item in iter_instance_types(cloud.instance_types())
    }
    now = time.time()
    for instance in cloud.instances():
        tags = tag_map(instance)
        launched = float(tags.get("lcloud-launched-at", now))
        instance_type_value = instance.get("instance_type")
        if isinstance(instance_type_value, dict):
            instance_type = instance_type_value.get("name", "?")
        else:
            instance_type = instance.get("instance_type_name") or instance_type_value or "?"
        rate = prices.get(str(instance_type))
        cost = estimate_compute_cost(rate, now - launched) if rate is not None else None
        cost_text = f"~${cost:.2f}" if cost is not None else "cost unknown"
        print(
            f"{instance.get('id')}  {instance.get('status')}  {instance_type}  "
            f"{instance.get('ip', '-')}  {cost_text}"
        )
        if key and instance.get("ip"):
            print(f"  {Remote(instance['ip'], key).ssh_command()}")


def remote_for_target(
    target: str,
    key: str,
    *,
    user: str = "ubuntu",
) -> Remote:
    if "." in target or ":" in target:
        return Remote(target, key, user=user)
    cloud = LambdaCloud()
    instance = cloud.instance(target)
    if not instance.get("ip"):
        raise SystemExit(f"Instance {target} has no public IP")
    return Remote(instance["ip"], key, user=user)


def confirm(message: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    answer = input(f"{message} Type 'terminate' to continue: ")
    if answer != "terminate":
        raise SystemExit("Cancelled")


def command_pull(
    remote: Remote,
    source: str,
    destination: str,
    *,
    every: float | None,
    delete: bool,
) -> None:
    if every is not None and every <= 0:
        raise SystemExit("--every must be positive")
    if every is None:
        remote.rsync_from(source, destination, delete=delete)
        return

    while True:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{started}] pulling {remote.target}:{source} -> {destination}")
        try:
            remote.rsync_from(source, destination, delete=delete)
        except subprocess.CalledProcessError as error:
            print(
                f"Pull failed with exit code {error.returncode}; retrying in {every:g}s.",
                file=sys.stderr,
            )
        time.sleep(every)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "offers":
        cloud = LambdaCloud()
        command_offers(cloud, only_available=args.available)
    elif args.command == "ssh-keys":
        cloud = LambdaCloud()
        for key in cloud.ssh_keys():
            print(f"{key.get('name')}  {key.get('id')}")
    elif args.command == "run":
        cloud = LambdaCloud()
        spec = JobSpec.from_json(args.spec)
        if args.max_cost is not None:
            _, price = Runner(cloud).resolve_offer(spec)
            estimate = estimate_compute_cost(
                price, spec.timeout_seconds + spec.setup_allowance_seconds
            )
            if estimate > args.max_cost:
                raise SystemExit(
                    f"Refusing launch: estimated compute cost ${estimate:.2f} "
                    f"exceeds --max-cost ${args.max_cost:.2f}"
                )
        Runner(cloud).run(
            spec,
            assume_yes=args.yes,
            follow_output=not args.detach,
        )
    elif args.command == "session":
        cloud = LambdaCloud()
        spec = SessionSpec.from_json(args.spec)
        if args.max_cost is not None:
            _, price = Runner(cloud).resolve_offer(spec)
            estimate = estimate_compute_cost(price, spec.max_lifetime_seconds)
            if estimate > args.max_cost:
                raise SystemExit(
                    f"Refusing launch: estimated compute cost ${estimate:.2f} "
                    f"exceeds --max-cost ${args.max_cost:.2f}"
                )
        Runner(cloud).session(spec, assume_yes=args.yes)
    elif args.command == "list":
        cloud = LambdaCloud()
        command_list(cloud, args.key)
    elif args.command == "terminate":
        cloud = LambdaCloud()
        confirm(f"Terminate {', '.join(args.instance_ids)}?", args.yes)
        cloud.terminate(args.instance_ids)
    elif args.command == "reconcile":
        cloud = LambdaCloud()
        expired = []
        now = time.time()
        for instance in cloud.instances():
            tags = tag_map(instance)
            deadline = tags.get("lcloud-deadline")
            if tags.get("lcloud-managed") == "true" and deadline and float(deadline) <= now:
                expired.append(str(instance["id"]))
        if not expired:
            print("No expired lcloud instances.")
        else:
            confirm(f"Terminate expired instances {', '.join(expired)}?", args.yes)
            cloud.terminate(expired)
    elif args.command == "ssh":
        print(remote_for_target(args.target, args.key).ssh_command())
    elif args.command == "exec":
        command_parts = list(args.remote_command)
        if command_parts and command_parts[0] == "--":
            command_parts = command_parts[1:]
        if not command_parts:
            raise SystemExit("Missing remote command. Example: lcloud exec INSTANCE --key KEY -- nvidia-smi")
        command = " ".join(shlex.quote(part) for part in command_parts)
        if args.cwd:
            command = f"cd -- {shlex.quote(args.cwd)} && {command}"
        remote_for_target(args.target, args.key, user=args.user).command(command)
    elif args.command == "push":
        remote_for_target(args.target, args.key, user=args.user).rsync_to(
            args.source, args.destination
        )
    elif args.command == "pull":
        command_pull(
            remote_for_target(args.target, args.key, user=args.user),
            args.source,
            args.destination,
            every=args.every,
            delete=args.delete,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        raise SystemExit(130)
