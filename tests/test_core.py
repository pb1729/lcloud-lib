import os
import io
import json
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from lcloud.api import (
    LambdaCloud,
    LambdaCloudTransientError,
    available_regions,
    hourly_price,
    instance_type_name,
    iter_instance_types,
)
from lcloud.runner import (
    JobSpec,
    Runner,
    SessionSpec,
    emergency_script,
    estimate_compute_cost,
)
from lcloud.supervisor import write_status
from lcloud.remote import Remote


class ApiShapeTests(unittest.TestCase):
    def test_mapping_instance_types(self):
        payload = {
            "gpu_1x_a10": {
                "instance_type": {"price_cents_per_hour": 75},
                "regions_with_capacity_available": [{"name": "us-east-1"}],
            }
        }
        item = iter_instance_types(payload)[0]
        self.assertEqual(instance_type_name(item), "gpu_1x_a10")
        self.assertEqual(hourly_price(item), 0.75)
        self.assertEqual(available_regions(item), ["us-east-1"])

    def test_list_instance_types(self):
        item = {
            "name": "gpu_1x_test",
            "price_cents_per_hour": 123,
            "regions_with_capacity_available": ["us-west-1"],
        }
        self.assertEqual(iter_instance_types([item]), [item])
        self.assertEqual(hourly_price(item), 1.23)

    @patch("lcloud.api.time.sleep")
    @patch("lcloud.api.urllib.request.urlopen")
    def test_get_retries_transient_connection_failure(self, urlopen, sleep):
        urlopen.side_effect = [
            urllib.error.URLError(ConnectionResetError(104, "reset")),
            io.BytesIO(b'{"data": {"status": "active"}}'),
        ]
        cloud = LambdaCloud("key", base_url="https://example.test")
        self.assertEqual(cloud.instance("abc"), {"status": "active"})
        self.assertEqual(urlopen.call_count, 2)

    @patch("lcloud.api.time.sleep")
    @patch("lcloud.api.urllib.request.urlopen")
    def test_launch_is_not_blindly_retried(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.URLError(
            ConnectionResetError(104, "reset")
        )
        cloud = LambdaCloud("key", base_url="https://example.test")
        with self.assertRaises(LambdaCloudTransientError):
            cloud.launch(
                region="us-test-1",
                instance_type="gpu",
                ssh_key_names=["key"],
                name="test",
            )
        self.assertEqual(urlopen.call_count, 1)

    @patch("lcloud.api.urllib.request.urlopen")
    def test_launch_accepts_multiple_ssh_keys(self, urlopen):
        urlopen.return_value = io.BytesIO(
            b'{"data": {"instance_ids": ["instance-1"]}}'
        )
        cloud = LambdaCloud("key", base_url="https://example.test")
        self.assertEqual(
            cloud.launch(
                region="us-test-1",
                instance_type="gpu",
                ssh_key_names=["key-a", "key-b"],
                name="test",
            ),
            "instance-1",
        )
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(body["ssh_key_names"], ["key-a", "key-b"])

    @patch("lcloud.api.urllib.request.urlopen")
    def test_launch_still_accepts_singular_ssh_key(self, urlopen):
        urlopen.return_value = io.BytesIO(
            b'{"data": {"instance_ids": ["instance-1"]}}'
        )
        cloud = LambdaCloud("key", base_url="https://example.test")
        cloud.launch(
            region="us-test-1",
            instance_type="gpu",
            ssh_key_name="key-a",
            name="test",
        )
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(body["ssh_key_names"], ["key-a"])

    @patch("lcloud.api.time.sleep")
    @patch("lcloud.api.urllib.request.urlopen")
    def test_terminate_retries_transient_connection_failure(self, urlopen, sleep):
        urlopen.side_effect = [
            urllib.error.URLError(ConnectionResetError(104, "reset")),
            io.BytesIO(b'{"data": {"terminated_instances": []}}'),
        ]
        cloud = LambdaCloud("key", base_url="https://example.test")
        self.assertEqual(
            cloud.terminate(["abc"]),
            {"terminated_instances": []},
        )
        self.assertEqual(urlopen.call_count, 2)


class RunnerTests(unittest.TestCase):
    def test_cost_rounds_to_minute(self):
        self.assertAlmostEqual(estimate_compute_cost(1.20, 61), 0.04)

    def test_checkpoint_requires_filesystem(self):
        spec = JobSpec.from_dict(
            {
                "command": "true",
                "instance_type": "gpu",
                "ssh_key_name": "key",
                "ssh_private_key": "key.pem",
                "timeout_seconds": 1,
                "checkpoint": {"source": "/tmp/a", "destination": "a"},
            }
        )
        with self.assertRaisesRegex(ValueError, "requires a filesystem"):
            spec.validate()

    def test_setup_allowance_must_be_positive(self):
        spec = JobSpec(
            command="true",
            instance_type="gpu",
            ssh_key_name="key",
            ssh_private_key="key.pem",
            timeout_seconds=1,
            setup_allowance_seconds=0,
        )
        with self.assertRaisesRegex(ValueError, "setup_allowance_seconds"):
            spec.validate()

    def test_job_accepts_plural_ssh_key_names(self):
        spec = JobSpec.from_dict(
            {
                "command": "true",
                "instance_type": "gpu",
                "ssh_key_names": ["key-a", "key-b"],
                "ssh_private_key": "key.pem",
                "timeout_seconds": 1,
            }
        )
        self.assertEqual(spec.lambda_ssh_key_names(), ["key-a", "key-b"])

    def test_job_rejects_singular_and_plural_ssh_keys(self):
        spec = JobSpec(
            command="true",
            instance_type="gpu",
            ssh_key_name="key-a",
            ssh_key_names=["key-b"],
            ssh_private_key="key.pem",
            timeout_seconds=1,
        )
        with self.assertRaisesRegex(ValueError, "either ssh_key_name or ssh_key_names"):
            spec.validate()

    def test_session_lifetime_must_be_positive(self):
        spec = SessionSpec(
            instance_type="gpu",
            ssh_key_name="key",
            ssh_private_key="key.pem",
            max_lifetime_seconds=0,
        )
        with self.assertRaisesRegex(ValueError, "max_lifetime_seconds"):
            spec.validate()

    def test_session_rejects_empty_setup_commands(self):
        spec = SessionSpec(
            instance_type="gpu",
            ssh_key_name="key",
            ssh_private_key="key.pem",
            max_lifetime_seconds=3600,
            setup_commands=["true", " "],
        )
        with self.assertRaisesRegex(ValueError, "setup_commands"):
            spec.validate()

    def test_emergency_script_is_instance_bound(self):
        script = emergency_script("https://example.test/api/v1")
        self.assertIn("/etc/lcloud/instance-id", script)
        self.assertIn("/instance-operations/terminate", script)
        self.assertIn("--retry-all-errors", script)

    def test_storage_rate_comes_from_environment(self):
        class Cloud:
            def file_systems(self):
                return [{"name": "research", "bytes_used": 10 * 1024**3}]

        spec = JobSpec(
            command="true",
            instance_type="gpu",
            ssh_key_name="key",
            ssh_private_key="key.pem",
            timeout_seconds=1,
            filesystem="research",
        )
        with patch.dict(
            os.environ, {"LCLOUD_STORAGE_RATE_PER_GIB_MONTH": "0.20"}, clear=False
        ):
            summary = Runner(Cloud()).storage_summary(spec)
        self.assertIn("$2.00/month", summary)
        self.assertIn("configured rate $0.2000/GiB-month", summary)

    def test_storage_rate_is_optional(self):
        class Cloud:
            def file_systems(self):
                return [{"name": "research", "bytes_used": 0}]

        spec = JobSpec(
            command="true",
            instance_type="gpu",
            ssh_key_name="key",
            ssh_private_key="key.pem",
            timeout_seconds=1,
            filesystem="research",
        )
        with patch.dict(os.environ, {}, clear=True):
            summary = Runner(Cloud()).storage_summary(spec)
        self.assertIn("rate unavailable from Lambda API", summary)

    def test_missing_ssh_key_fails_before_launch(self):
        class Cloud:
            def instance_types(self):
                return {
                    "gpu": {
                        "instance_type": {"price_cents_per_hour": 50},
                        "regions_with_capacity_available": ["us-test-1"],
                    }
                }

            def ssh_keys(self):
                return [{"name": "actual-key"}]

        spec = JobSpec(
            command="true",
            instance_type="gpu",
            ssh_key_name="wrong-key",
            ssh_private_key="key.pem",
            timeout_seconds=1,
        )
        with self.assertRaisesRegex(Exception, "actual-key"):
            Runner(Cloud()).run(spec, assume_yes=True)

    def test_filesystem_region_constrains_offer(self):
        class Cloud:
            def file_systems(self):
                return [
                    {"name": "research", "region": {"name": "us-midwest-2"}}
                ]

            def instance_types(self):
                return {
                    "gpu": {
                        "instance_type": {"price_cents_per_hour": 50},
                        "regions_with_capacity_available": [
                            "us-east-1",
                            "us-midwest-2",
                        ],
                    }
                }

        spec = JobSpec(
            command="true",
            instance_type="gpu",
            ssh_key_name="key",
            ssh_private_key="key.pem",
            timeout_seconds=1,
            filesystem="research",
        )
        self.assertEqual(Runner(Cloud()).resolve_offer(spec), ("us-midwest-2", 0.50))

    def test_explicit_region_must_match_filesystem(self):
        class Cloud:
            def file_systems(self):
                return [
                    {"name": "research", "region": {"name": "us-midwest-2"}}
                ]

        spec = JobSpec(
            command="true",
            instance_type="gpu",
            ssh_key_name="key",
            ssh_private_key="key.pem",
            timeout_seconds=1,
            filesystem="research",
            region="us-east-1",
        )
        with self.assertRaisesRegex(Exception, "does not match filesystem"):
            Runner(Cloud()).resolve_offer(spec)

    def test_status_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            local_status = os.path.join(directory, "local-status.json")
            destination = os.path.join(directory, "persistent")
            config = {"status_destination": destination}
            status = {"return_code": 2, "final_sync_error": "copy failed"}
            with patch("lcloud.supervisor.STATUS_PATH", local_status):
                write_status(config, status)
            persisted = os.path.join(destination, "lcloud-status.json")
            with open(persisted, encoding="utf-8") as file:
                self.assertEqual(json.load(file), status)

    @patch("lcloud.remote.time.sleep")
    @patch("lcloud.remote.subprocess.run")
    def test_setup_ssh_retries_connection_resets(self, run, sleep):
        run.side_effect = [Mock(returncode=255), Mock(returncode=0)]
        Remote("192.0.2.1", "key.pem").command("true")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(5)

    @patch("lcloud.remote.time.sleep")
    @patch("lcloud.remote.subprocess.run")
    def test_ssh_wait_requires_stability(self, run, sleep):
        run.side_effect = [
            Mock(returncode=0),
            Mock(returncode=255),
            Mock(returncode=0),
            Mock(returncode=0),
            Mock(returncode=0),
        ]
        Remote("192.0.2.1", "key.pem").wait(timeout=30)
        self.assertEqual(run.call_count, 5)

    @patch("lcloud.remote.subprocess.run")
    def test_rsync_from_can_delete_for_mirroring(self, run):
        run.return_value = Mock(returncode=0)
        Remote("192.0.2.1", "key.pem").rsync_from(
            "/remote/checkpoints/", "./checkpoints/", delete=True
        )
        argv = run.call_args.args[0]
        self.assertIn("--delete-delay", argv)
        self.assertIn("ubuntu@192.0.2.1:/remote/checkpoints/", argv)


if __name__ == "__main__":
    unittest.main()
