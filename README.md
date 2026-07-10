# lcloud-lib

A deliberately small personal experiment runner for Lambda Cloud. It launches one VM,
uploads local files, runs one command under a remote `systemd` supervisor, periodically
copies checkpoints to an attached Lambda filesystem, and terminates the VM through the
Lambda API.

It can also launch a bounded setup session: an instance that stays on for interactive
SSH/manual experimentation until you terminate it or an explicit max-lifetime guard
fires.

The safety policy is fail-closed:

- Normal completion or timeout gets a final bounded checkpoint sync, then termination.
- A supervisor crash or unexpected reboot skips checkpoint work and immediately retries
  API termination.
- Local setup failures terminate the instance before returning.
- A remote setup timer is armed before uploads, so losing the laptop during `rsync`
  terminates the instance when the setup allowance expires.
- `lcloud reconcile` terminates managed instances whose tagged deadline has passed.

Transient control-plane failures on safe API operations are retried with bounded
exponential backoff. Launch requests are never blindly repeated: if a launch response is
lost, lcloud searches for the unique run tag to avoid creating a duplicate billable VM.

Before uploading code, lcloud verifies that the requested filesystem is actually mounted
and writable by the job user. At completion it writes `lcloud-status.json` beside the
persisted artifacts with the command return code, timeout state, and final-sync error.

An entirely frozen VM cannot terminate itself. Run `lcloud reconcile --yes` periodically
from another machine if that failure mode needs coverage.

## Install

Python 3.10+, `ssh`, and `rsync` are required locally.

```bash
python3 -m pip install -e .
export LAMBDA_API_KEY=...
```

## Job file

```json
{
  "name": "mnist",
  "instance_type": "gpu_1x_a10",
  "ssh_key_names": ["my-lambda-key", "backup-key"],
  "ssh_private_key": "~/.ssh/id_ed25519",
  "timeout_seconds": 14400,
  "setup_allowance_seconds": 900,
  "command": "source .venv/bin/activate && python train.py",
  "workdir": "/home/ubuntu/project",
  "job_user": "ubuntu",
  "region": null,
  "filesystem": "research",
  "uploads": [
    {
      "source": "./",
      "destination": "/home/ubuntu/project/"
    }
  ],
  "checkpoint": {
    "source": "/home/ubuntu/project/checkpoints",
    "destination": "runs/mnist/checkpoints",
    "interval_seconds": 300,
    "sync_timeout_seconds": 300,
    "exclude": ["*.tmp", "*.partial"]
  }
}
```

`checkpoint.destination` is relative to the attached filesystem. Checkpoints should be
written to a temporary name and atomically renamed when complete; `rsync` does not create
a consistent snapshot of files that are still being modified.

Use `ssh_key_names` to authorize one or more Lambda SSH public keys on the launched
instance. The older singular `ssh_key_name` field still works for one-key configs, but
do not set both fields in the same spec.

## Commands

```bash
lcloud offers --available
lcloud ssh-keys
lcloud run job.json
lcloud run job.json --yes --max-cost 6.00
lcloud run job.json --detach
lcloud session session.json
lcloud list --key ~/.ssh/id_ed25519
lcloud ssh INSTANCE_ID_OR_IP --key ~/.ssh/id_ed25519
lcloud exec INSTANCE_ID_OR_IP --key ~/.ssh/id_ed25519 -- nvidia-smi
lcloud exec INSTANCE_ID_OR_IP --key ~/.ssh/id_ed25519 -- bash -lc 'cd project && python setup_check.py'
lcloud push INSTANCE_ID_OR_IP ./local-script.py /home/ubuntu/project/ --key ~/.ssh/id_ed25519
lcloud pull INSTANCE_ID_OR_IP /home/ubuntu/project/output/ ./output/ --key ~/.ssh/id_ed25519
lcloud pull INSTANCE_ID_OR_IP /home/ubuntu/project/checkpoints/ ./checkpoints/ --key ~/.ssh/id_ed25519 --every 60
lcloud reconcile
lcloud terminate INSTANCE_ID
```

The compute estimate includes the configured setup allowance and rounds up to Lambda's
one-minute billing increment. Storage output always reports API-provided current usage.
Lambda's public API does not expose the storage rate; optionally configure the rate shown
in the console once for all jobs:

```bash
export LCLOUD_STORAGE_RATE_PER_GIB_MONTH=0.20
```

When configured, lcloud also displays the current monthly storage run rate. Future
checkpoint size remains unknown.

By default, `lcloud run` follows the remote service journal after setup. The job remains
independent: pressing Ctrl-C only stops local output following. Use `--detach` to return
immediately after remote startup.

## Repeated checkpoint pulls

`lcloud pull` uses `rsync`, so it can copy files or entire directories. Use trailing
slashes deliberately:

- `/remote/checkpoints/ ./checkpoints/` copies the contents of the remote directory.
- `/remote/checkpoints ./` copies the `checkpoints` directory itself into `./`.

To keep a local checkpoint mirror updated while training runs, start this inside `tmux`
on any machine with SSH access to the instance:

```bash
lcloud pull INSTANCE_ID_OR_IP /home/ubuntu/project/checkpoints/ ./checkpoints/ \
  --key ~/.ssh/id_ed25519 \
  --every 60
```

The command pulls once immediately, then repeats every 60 seconds until Ctrl-C. In
repeated mode, a failed pull is logged and retried on the next interval. Add `--delete`
only if you want the local directory to mirror remote deletions too.

## Setup sessions

Use `lcloud session` when you want to keep one instance alive while figuring out setup
commands. A session spec is like a job spec without a training command:

```json
{
  "name": "setup",
  "instance_type": "gpu_1x_a10",
  "region": "us-east-1",
  "ssh_key_names": ["my-lambda-key", "backup-key"],
  "ssh_private_key": "~/.ssh/id_ed25519",
  "max_lifetime_seconds": 14400,
  "filesystem": "research",
  "uploads": [
    {
      "source": "./",
      "destination": "/home/ubuntu/project/"
    }
  ],
  "setup_commands": [
    "mkdir -p /home/ubuntu/project"
  ]
}
```

After the session is ready, use ordinary SSH for interactive work or `lcloud exec` for
one-shot commands. Edit files locally, then `lcloud push` them back to the instance.
If another agent needs to control the session, point it at
[`docs/agent-remote-control.md`](docs/agent-remote-control.md).
When done, terminate explicitly:

```bash
lcloud terminate INSTANCE_ID
```

## Security

Do not run untrusted workloads with this design. Probably also don't do anything that talks to the web a lot.
