# CUDA MNIST live test

This example trains a larger convolutional network for up to 1,000 epochs and refuses
to run without CUDA. The intentionally large epoch count keeps the job alive for manual
SSH inspection; the lcloud timeout normally ends it first. `torchvision.datasets.MNIST`
with `download=True` stores the dataset on the attached Lambda filesystem and reuses it
on later runs.

The script maintains 20 rotating checkpoint slots, atomically updating one every 30
seconds. The lcloud supervisor copies those checkpoints and `training.log` to:

```text
/lambda/nfs/<filesystem>/runs/lcloud-mnist/artifacts/
```

The artifact directory also contains `lcloud-status.json`, which records whether the
training command succeeded and whether the final checkpoint sync encountered an error.

Copy `job.json.example` to `job.json`, replace the three uppercase placeholders, then
run from the repository root:

```bash
lcloud run examples/mnist/job.json
```

For interactive setup experiments, copy `session.json.example` to `session.json` and
launch:

```bash
lcloud session examples/mnist/session.json
lcloud exec INSTANCE_ID_OR_IP --key ~/.ssh/id_ed25519 -- nvidia-smi
lcloud push INSTANCE_ID_OR_IP examples/mnist/train.py /home/ubuntu/lcloud-mnist/ --key ~/.ssh/id_ed25519
```

The local output directory is ephemeral. Dataset files and synchronized artifacts are
on the persistent filesystem before the instance is terminated.
