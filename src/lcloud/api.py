from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://cloud.lambda.ai/api/v1"


class LambdaCloudError(RuntimeError):
    pass


class LambdaCloudHTTPError(LambdaCloudError):
    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(
            f"Lambda API {method} {path} failed ({status}): {detail}"
        )


class LambdaCloudTransientError(LambdaCloudError):
    """A transport/server failure where the request outcome may be unknown."""

    pass


class LambdaCloud:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30,
    ) -> None:
        self.api_key = api_key or os.environ.get("LAMBDA_API_KEY", "")
        if not self.api_key:
            raise LambdaCloudError("LAMBDA_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._last_request_at = 0.0

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        retryable_operation = method == "GET" or path == "/instance-operations/terminate"
        attempts = 8 if path == "/instance-operations/terminate" else 6
        if not retryable_operation:
            attempts = 1

        for attempt in range(1, attempts + 1):
            # Lambda documents a general limit of roughly one request per second.
            wait = 1.05 - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            request = urllib.request.Request(
                f"{self.base_url}{path}",
                data=data,
                method=method,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "lcloud-lib/0.1",
                },
            )
            self._last_request_at = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                return payload.get("data", payload)
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")
                transient = error.code == 429 or 500 <= error.code <= 599
                if not transient:
                    raise LambdaCloudHTTPError(
                        method, path, error.code, detail
                    ) from error
                if not retryable_operation:
                    if error.code >= 500:
                        raise LambdaCloudTransientError(
                            f"Lambda API {method} {path} had an uncertain outcome "
                            f"after HTTP {error.code}: {detail}"
                        ) from error
                    raise LambdaCloudHTTPError(
                        method, path, error.code, detail
                    ) from error
                if attempt >= attempts:
                    raise LambdaCloudHTTPError(
                        method, path, error.code, detail
                    ) from error
                retry_after = error.headers.get("Retry-After") if error.headers else None
                delay = _retry_delay(attempt, retry_after)
                _report_retry(method, path, attempt, attempts, error.code, delay)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                if not (retryable_operation and attempt < attempts):
                    reason = getattr(error, "reason", error)
                    raise LambdaCloudTransientError(
                        f"Lambda API {method} {path} had an uncertain outcome: {reason}"
                    ) from error
                delay = _retry_delay(attempt)
                _report_retry(method, path, attempt, attempts, error, delay)
                time.sleep(delay)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                if not (retryable_operation and attempt < attempts):
                    raise LambdaCloudTransientError(
                        f"Lambda API {method} {path} returned an invalid response: {error}"
                    ) from error
                delay = _retry_delay(attempt)
                _report_retry(method, path, attempt, attempts, "invalid response", delay)
                time.sleep(delay)

        raise AssertionError("unreachable")

    def instance_types(self) -> Any:
        return self.request("GET", "/instance-types")

    def instances(self) -> list[dict[str, Any]]:
        return self.request("GET", "/instances")

    def instance(self, instance_id: str) -> dict[str, Any]:
        return self.request("GET", f"/instances/{instance_id}")

    def file_systems(self) -> list[dict[str, Any]]:
        return self.request("GET", "/file-systems")

    def ssh_keys(self) -> list[dict[str, Any]]:
        return self.request("GET", "/ssh-keys")

    def launch(
        self,
        *,
        region: str,
        instance_type: str,
        ssh_key_name: str,
        name: str,
        file_system_names: list[str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "region_name": region,
            "instance_type_name": instance_type,
            "ssh_key_names": [ssh_key_name],
            "name": name,
        }
        if file_system_names:
            body["file_system_names"] = file_system_names
        if tags:
            body["tags"] = [{"key": key, "value": value} for key, value in tags.items()]
        result = self.request("POST", "/instance-operations/launch", body=body)
        ids = result.get("instance_ids", [])
        if len(ids) != 1:
            raise LambdaCloudError(f"Expected one launched instance, got: {ids!r}")
        return ids[0]

    def terminate(self, instance_ids: list[str]) -> Any:
        return self.request(
            "POST",
            "/instance-operations/terminate",
            body={"instance_ids": instance_ids},
        )

    def wait_until_active(
        self,
        instance_id: str,
        *,
        timeout: float = 600,
        interval: float = 10,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                instance = self.instance(instance_id)
            except LambdaCloudHTTPError as error:
                # A newly returned launch ID can briefly precede its readable record.
                if error.status == 404:
                    time.sleep(interval)
                    continue
                raise
            status = instance.get("status")
            if status == "active" and instance.get("ip"):
                return instance
            if status in {"terminated", "terminating", "unhealthy", "preempted"}:
                raise LambdaCloudError(
                    f"Instance {instance_id} entered status {status!r} while starting"
                )
            time.sleep(interval)
        raise TimeoutError(f"Instance {instance_id} did not become active in time")


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(30.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(15.0, float(2 ** (attempt - 1)))


def _report_retry(
    method: str,
    path: str,
    attempt: int,
    attempts: int,
    reason: object,
    delay: float,
) -> None:
    print(
        f"Lambda API transient failure during {method} {path} ({reason}); "
        f"retrying in {delay:g}s ({attempt}/{attempts})...",
        file=sys.stderr,
    )


def iter_instance_types(payload: Any) -> list[dict[str, Any]]:
    """Normalize the two response shapes used by Lambda's instance-types API."""
    if isinstance(payload, dict):
        result = []
        for name, value in payload.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("name", name)
                result.append(item)
        return result
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    return []


def instance_type_name(item: dict[str, Any]) -> str:
    nested = item.get("instance_type") or {}
    return str(item.get("name") or nested.get("name") or "")


def hourly_price(item: dict[str, Any]) -> float | None:
    nested = item.get("instance_type") or {}
    cents = item.get("price_cents_per_hour", nested.get("price_cents_per_hour"))
    if cents is not None:
        return float(cents) / 100
    dollars = item.get("price_per_hour", nested.get("price_per_hour"))
    return float(dollars) if dollars is not None else None


def available_regions(item: dict[str, Any]) -> list[str]:
    regions = item.get("regions_with_capacity_available", [])
    result = []
    for region in regions:
        if isinstance(region, str):
            result.append(region)
        elif isinstance(region, dict) and region.get("name"):
            result.append(str(region["name"]))
    return result
