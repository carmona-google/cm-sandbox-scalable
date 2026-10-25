"""Unit tests for the CodeMender GKE Agent Sandbox Flask Orchestrator."""

import tempfile
from typing import Any, Dict, List
import unittest

from orchestrator.app import create_app
from orchestrator.sandbox_manager import (
    ScanJob,
    SandboxOrchestrator,
    build_sandbox_claim_manifest,
    build_worker_command,
    validate_scan_request,
)


class FakeSandboxBackend:
    """In-memory fake Kubernetes backend for testing SandboxOrchestrator."""

    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.created_claims: List[str] = []
        self.deleted_claims: List[str] = []
        self.executed_jobs: List[ScanJob] = []

    def create_claim(self, claim_name: str, scan_id: str) -> Dict[str, Any]:
        self.created_claims.append(claim_name)
        return {"metadata": {"name": claim_name}}

    def wait_for_bound_pod(self, claim_name: str, scan_id: str) -> str:
        if self.should_fail:
            raise TimeoutError("Simulated warm pool timeout")
        return f"codemender-warmpool-pod-{claim_name}"

    def exec_scan_in_pod(self, pod_name: str, job: ScanJob, on_progress: Any = None) -> Dict[str, Any]:
        self.executed_jobs.append(job)
        if on_progress:
            on_progress({
                "state": "SCANNING",
                "message": "Running CodeMender on local SSD",
                "staging_duration_seconds": 2,
                "scan_duration_seconds": 5,
            })
        return {
            "exit_code": 0,
            "staging_duration_seconds": 2,
            "scan_duration_seconds": 18,
            "message": "Scan completed successfully on local emptyDir",
        }

    def delete_claim(self, claim_name: str) -> None:
        self.deleted_claims.append(claim_name)


class OrchestratorTestCase(unittest.TestCase):
    """Tests validation, command generation, and Flask API endpoints."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = f"{self.temp_dir.name}/scans.json"
        self.backend = FakeSandboxBackend()
        self.orchestrator = SandboxOrchestrator(
            backend=self.backend,
            out_bucket="demo-cm-out-bucket",
            run_async=False,
            state_file=self.state_file,
        )
        self.app = create_app(self.orchestrator)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_healthz_endpoint(self) -> None:
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")

    def test_validate_scan_request_prevents_traversal_and_invalid_modes(self) -> None:
        with self.assertRaises(ValueError):
            validate_scan_request({"target_path": "../../etc/passwd"})

        with self.assertRaises(ValueError):
            validate_scan_request({"target_path": "my-repo; rm -rf /"})

        with self.assertRaises(ValueError):
            validate_scan_request({"target_path": "my-repo", "scan_mode": "invalid"})

        with self.assertRaises(ValueError):
            validate_scan_request({"target_path": "my-repo", "workers": 64})

        valid = validate_scan_request({
            "target_path": "/my-repo/service-a",
            "scan_mode": "deep",
            "model": "gemini-4-Argon",
            "workers": 8,
        })
        self.assertEqual(valid["target_path"], "my-repo/service-a")
        self.assertEqual(valid["scan_mode"], "deep")
        self.assertEqual(valid["model"], "gemini-4-argon")
        self.assertEqual(valid["workers"], 8)

    def test_build_worker_command_and_claim_manifest(self) -> None:
        job = ScanJob(
            scan_id="scan-100-abcd1234",
            target_path="my-app",
            scan_mode="deep",
            model="gemini-3.8-flash",
            workers=8,
            diff_ref="origin/main",
        )
        cmd = build_worker_command(job)
        self.assertEqual(cmd[0], "/bin/bash")
        self.assertIn("TARGET_SUBPATH=my-app", cmd[2])
        self.assertIn("SCAN_MODE=deep", cmd[2])
        self.assertIn("WORKERS=8", cmd[2])
        self.assertIn("/usr/local/bin/run_scan.sh", cmd[2])

        manifest = build_sandbox_claim_manifest(
            claim_name="cm-claim-abcd1234",
            namespace="default",
            warmpool_name="codemender-warmpool",
            scan_id=job.scan_id,
        )
        self.assertEqual(manifest["apiVersion"], "extensions.agents.x-k8s.io/v1beta1")
        self.assertEqual(manifest["kind"], "SandboxClaim")
        self.assertEqual(
            manifest["spec"]["warmPoolRef"]["name"], "codemender-warmpool"
        )

    def test_create_get_and_list_scans_lifecycle(self) -> None:
        resp = self.client.post(
            "/api/v1/scans",
            json={
                "target_path": "sample-repo",
                "scan_mode": "deep",
                "model": "gemini-3.8-flash",
                "workers": 8,
                "auto_release": True,
            },
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        scan_id = body["scan_id"]
        self.assertEqual(body["state"], "COMPLETED")
        self.assertEqual(body["staging_duration_seconds"], 2)
        self.assertEqual(body["scan_duration_seconds"], 18)
        self.assertTrue(
            body["reports"]["sarif"].startswith(
                f"gs://demo-cm-out-bucket/{scan_id}/findings.sarif"
            )
        )
        # Verify claim was created and auto-released after completion
        self.assertEqual(len(self.backend.created_claims), 1)
        self.assertEqual(self.backend.created_claims, self.backend.deleted_claims)

        # Verify GET /api/v1/scans/<scan_id> across a second orchestrator instance sharing state_file
        second_orchestrator = SandboxOrchestrator(
            backend=self.backend,
            out_bucket="demo-cm-out-bucket",
            run_async=False,
            state_file=self.state_file,
        )
        second_client = create_app(second_orchestrator).test_client()
        get_resp = second_client.get(f"/api/v1/scans/{scan_id}")
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.get_json()["scan_id"], scan_id)

        # Verify GET /api/v1/scans
        list_resp = second_client.get("/api/v1/scans")
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(list_resp.get_json()["count"], 1)

    def test_delete_scan_releases_claim(self) -> None:
        resp = self.client.post(
            "/api/v1/scans",
            json={"target_path": "sample-repo", "auto_release": False},
        )
        scan_id = resp.get_json()["scan_id"]
        self.assertEqual(len(self.backend.deleted_claims), 0)

        del_resp = self.client.delete(f"/api/v1/scans/{scan_id}")
        self.assertEqual(del_resp.status_code, 200)
        self.assertEqual(len(self.backend.deleted_claims), 1)


if __name__ == "__main__":
    unittest.main()
