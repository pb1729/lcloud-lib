#!/usr/bin/env python3
"""Run a long-lived CUDA MNIST training job with rotating checkpoints."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import torch
from torch import nn
from torch.utils.data import DataLoader
from torchvision import datasets, transforms


class Network(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(1, 64, 3, padding=1),
            nn.ReLU(),
            nn.Conv2d(64, 128, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(128, 256, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Flatten(),
            nn.Linear(256 * 7 * 7, 512),
            nn.ReLU(),
            nn.Linear(512, 10),
        )

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.layers(inputs)


def atomic_checkpoint(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as file:
        torch.save(payload, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--checkpoint-count", type=int, default=20)
    parser.add_argument("--checkpoint-interval-seconds", type=float, default=30)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--epochs", type=int, default=1000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for this example")
    if args.checkpoint_count <= 0:
        raise ValueError("--checkpoint-count must be positive")
    if args.checkpoint_interval_seconds <= 0:
        raise ValueError("--checkpoint-interval-seconds must be positive")
    if args.epochs <= 0:
        raise ValueError("--epochs must be positive")

    torch.manual_seed(0)
    device = torch.device("cuda")
    print(f"torch={torch.__version__}")
    print(f"device={device} gpu={torch.cuda.get_device_name(0)}")
    print(f"data_dir={args.data_dir}")
    print(f"output_dir={args.output_dir}")

    dataset = datasets.MNIST(
        root=args.data_dir,
        train=True,
        download=True,
        transform=transforms.ToTensor(),
    )
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=2,
        pin_memory=True,
    )
    model = Network().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_function = nn.CrossEntropyLoss()
    model.train()
    started = time.monotonic()
    checkpoint_number = 0
    global_step = 0
    next_checkpoint = time.monotonic()
    for epoch in range(1, args.epochs + 1):
        epoch_loss = 0.0
        for step, (inputs, targets) in enumerate(loader, start=1):
            global_step += 1
            inputs = inputs.to(device, non_blocking=True)
            targets = targets.to(device, non_blocking=True)
            optimizer.zero_grad(set_to_none=True)
            predictions = model(inputs)
            loss = loss_function(predictions, targets)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()

            if time.monotonic() >= next_checkpoint:
                checkpoint_number += 1
                slot = ((checkpoint_number - 1) % args.checkpoint_count) + 1
                path = args.output_dir / "checkpoints" / f"checkpoint-{slot:02d}.pt"
                atomic_checkpoint(
                    path,
                    {
                        "epoch": epoch,
                        "step": step,
                        "global_step": global_step,
                        "checkpoint_number": checkpoint_number,
                        "model": model.state_dict(),
                        "optimizer": optimizer.state_dict(),
                    },
                )
                print(
                    f"checkpoint={checkpoint_number} slot={slot:02d}/{args.checkpoint_count} "
                    f"epoch={epoch}/{args.epochs} step={step}/{len(loader)} "
                    f"loss={loss.item():.4f}",
                    flush=True,
                )
                next_checkpoint = time.monotonic() + args.checkpoint_interval_seconds

        print(
            f"epoch={epoch}/{args.epochs} mean_loss={epoch_loss / len(loader):.4f} "
            f"elapsed_seconds={time.monotonic() - started:.1f}",
            flush=True,
        )

    elapsed = time.monotonic() - started
    print(
        f"complete epochs={args.epochs} batches_per_epoch={len(loader)} "
        f"elapsed_seconds={elapsed:.1f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
