# Agent remote-control guide

This project includes a small CLI for controlling an already-launched Lambda Cloud
setup session from a local agent process.

Use this guide when the user gives you an instance IP address or an `SSH:` line printed
by `lcloud session`.

## Inputs you need

- Instance IP address or hostname.
- SSH private key path, usually `/home/phillip/.ssh/id_ed25519`.
- Remote user, usually `ubuntu`.

Prefer the IP address over the Lambda instance ID. The IP path does not require
`LAMBDA_API_KEY` in the agent environment.

If the user gives a full SSH command like:

```bash
ssh -i /home/phillip/.ssh/id_ed25519 -o BatchMode=yes ubuntu@203.0.113.10
```

extract:

- key: `/home/phillip/.ssh/id_ed25519`
- user: `ubuntu`
- IP: `203.0.113.10`

## Run a remote command

For a simple argv-style command:

```bash
env/bin/lcloud exec 203.0.113.10 --key /home/phillip/.ssh/id_ed25519 -- nvidia-smi
```

For shell features such as `cd`, pipes, variables, globs, or redirection, explicitly run
a remote shell:

```bash
env/bin/lcloud exec 203.0.113.10 --key /home/phillip/.ssh/id_ed25519 -- bash -lc 'cd /home/ubuntu/project && ls -lah && python3 script.py --help'
```

You can also use `--cwd` for simple working-directory changes:

```bash
env/bin/lcloud exec 203.0.113.10 --key /home/phillip/.ssh/id_ed25519 --cwd /home/ubuntu/project -- python3 script.py --help
```

## Copy local files to the instance

```bash
env/bin/lcloud push 203.0.113.10 ./local_script.py /home/ubuntu/project/ --key /home/phillip/.ssh/id_ed25519
```

For directories, include trailing slashes deliberately:

```bash
env/bin/lcloud push 203.0.113.10 ./project/ /home/ubuntu/project/ --key /home/phillip/.ssh/id_ed25519
```

## Copy files back from the instance

```bash
env/bin/lcloud pull 203.0.113.10 /home/ubuntu/project/output/ ./output/ --key /home/phillip/.ssh/id_ed25519
```

Directory pulls use normal rsync trailing-slash behavior:

- `/remote/checkpoints/ ./checkpoints/` copies the directory contents.
- `/remote/checkpoints ./` copies the directory itself.

To keep pulling checkpoints while a job trains, run this in `tmux`:

```bash
env/bin/lcloud pull 203.0.113.10 /home/ubuntu/project/checkpoints/ ./checkpoints/ --key /home/phillip/.ssh/id_ed25519 --every 60
```

This pulls immediately, then repeats every 60 seconds until interrupted. In repeated
mode, failed pulls are logged and retried on the next interval. Add `--delete` only when
the local directory should mirror remote deletions.

## Print the manual SSH command

```bash
env/bin/lcloud ssh 203.0.113.10 --key /home/phillip/.ssh/id_ed25519
```

## Safety rules

- Do not terminate the instance unless the user asks, or unless you are following an
  explicit cleanup instruction.
- Assume the setup session has a max-lifetime guard, but do not rely on it for normal
  cleanup.
- Prefer direct IP commands from an agent environment. Instance-ID commands require
  `LAMBDA_API_KEY` because the CLI must look up the instance IP through the Lambda API.
- If a command needs pipes, redirection, environment variables, or compound shell logic,
  use `-- bash -lc '...'`.
- Be careful with quoting. The local CLI shell parses once, then the remote shell parses
  again when using `bash -lc`.

## Typical agent loop

1. Run a quick probe:

   ```bash
   env/bin/lcloud exec IP --key /home/phillip/.ssh/id_ed25519 -- nvidia-smi
   ```

2. Inspect remote state:

   ```bash
   env/bin/lcloud exec IP --key /home/phillip/.ssh/id_ed25519 -- bash -lc 'pwd; ls -lah /home/ubuntu; mount | grep lambda || true'
   ```

3. Edit scripts locally.

4. Push edited scripts:

   ```bash
   env/bin/lcloud push IP ./script.py /home/ubuntu/project/ --key /home/phillip/.ssh/id_ed25519
   ```

5. Run the candidate setup command remotely:

   ```bash
   env/bin/lcloud exec IP --key /home/phillip/.ssh/id_ed25519 -- bash -lc 'cd /home/ubuntu/project && python3 script.py'
   ```

6. Once the working sequence is known, copy only the load-bearing commands into the
   job or session configuration.
