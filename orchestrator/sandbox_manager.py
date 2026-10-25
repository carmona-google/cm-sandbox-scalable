"""Kubernetes Agent Sandbox lifecycle and CodeMender scan execution manager."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shlex
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_SCAN_MODES = ("quick", "deep", "diff")
SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9._/\-]+$")
SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9._\-]+$")
SAFE_REF_RE = re.compile(r"^[a-zA-Z0-9._/\-~^]+$")


@dataclass
class ScanJob:
    """Represents the state and metadata of a CodeMender scan job."""

    scan_id: str
    target_path: str
    scan_mode: str = "quick"
    model: str = ""
    workers: int = 4
    diff_ref: str = "HEAD"
    auto_release: bool = True
    claim_name: str = ""
    pod_name: str = ""
    state: str = "PENDING"
    message: str = "Queued for sandbox claim allocation"
    staging_duration_seconds: int = 0
    scan_duration_seconds: int = 0
    exit_code: Optional[int] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_dict(self, out_bucket: str = "") -> Dict[str, Any]:
        report_prefix = (
            f"gs://{out_bucket}/{self.scan_id}"
            if out_bucket
            else f"/workspace/out/{self.scan_id}"
        )
        return {
            "scan_id": self.scan_id,
            "target_path": self.target_path,
            "scan_mode": self.scan_mode,
            "model": self.model,
            "workers": self.workers,
            "diff_ref": self.diff_ref,
            "auto_release": self.auto_release,
            "claim_name": self.claim_name,
            "pod_name": self.pod_name,
            "state": self.state,
            "message": self.message,
            "staging_duration_seconds": self.staging_duration_seconds,
            "scan_duration_seconds": self.scan_duration_seconds,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reports": {
                "json": f"{report_prefix}/findings.json",
                "sarif": f"{report_prefix}/findings.sarif",
                "html": f"{report_prefix}/report.html",
                "markdown": f"{report_prefix}/report.md",
                "status": f"{report_prefix}/status.json",
                "log": f"{report_prefix}/scan.log",
            },
        }


def validate_scan_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validates and normalizes an incoming scan request payload."""
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")

    raw_target = str(payload.get("target_path", ".")).strip()
    if not raw_target or not SAFE_PATH_RE.match(raw_target):
        raise ValueError(
            "Invalid 'target_path'. Must contain only alphanumeric, '.', '_', '-', or '/'."
        )
    normalized_target = os.path.normpath(raw_target).lstrip("/")
    if normalized_target.startswith(".."):
        raise ValueError("Invalid 'target_path': path traversal ('..') is not allowed.")

    scan_mode = str(payload.get("scan_mode", "quick")).strip().lower()
    if scan_mode not in VALID_SCAN_MODES:
        raise ValueError(
            f"Invalid 'scan_mode' '{scan_mode}'. Allowed values: {', '.join(VALID_SCAN_MODES)}."
        )

    model = str(payload.get("model", "")).strip()
    if model and not SAFE_MODEL_RE.match(model):
        raise ValueError("Invalid 'model' identifier.")

    try:
        workers = int(payload.get("workers", 4))
    except (TypeError, ValueError) as exc:
        raise ValueError("'workers' must be an integer between 1 and 16.") from exc
    if workers < 1 or workers > 16:
        raise ValueError("'workers' must be between 1 and 16.")

    diff_ref = str(payload.get("diff_ref", "HEAD")).strip()
    if not SAFE_REF_RE.match(diff_ref):
        raise ValueError("Invalid 'diff_ref' git reference.")

    auto_release = bool(payload.get("auto_release", True))

    return {
        "target_path": normalized_target,
        "scan_mode": scan_mode,
        "model": model,
        "workers": workers,
        "diff_ref": diff_ref,
        "auto_release": auto_release,
    }


def build_sandbox_claim_manifest(
    claim_name: str,
    namespace: str,
    warmpool_name: str,
    scan_id: str,
) -> Dict[str, Any]:
    """Constructs the Kubernetes v1beta1 SandboxClaim custom resource manifest."""
    return {
        "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
        "kind": "SandboxClaim",
        "metadata": {
            "name": claim_name,
            "namespace": namespace,
            "labels": {
                "app": "codemender-orchestrator",
                "scan-id": scan_id,
            },
        },
        "spec": {
            "warmPoolRef": {
                "name": warmpool_name,
            }
        },
    }


def build_worker_command(job: ScanJob) -> List[str]:
    """Builds the headless worker execution command using local emptyDir staging."""
    env_assignments = [
        f"SCAN_ID={shlex.quote(job.scan_id)}",
        f"TARGET_SUBPATH={shlex.quote(job.target_path)}",
        f"SCAN_MODE={shlex.quote(job.scan_mode)}",
        f"MODEL={shlex.quote(job.model)}",
        f"WORKERS={int(job.workers)}",
        f"DIFF_REF={shlex.quote(job.diff_ref)}",
    ]
    cmd_str = " ".join(env_assignments) + " /usr/local/bin/run_scan.sh"
    return ["/bin/bash", "-c", cmd_str]


class K8sSandboxBackend:
    """Kubernetes API adapter for claiming warm-pool sandboxes and running scans."""

    GROUP = "extensions.agents.x-k8s.io"
    VERSION = "v1beta1"
    PLURAL = "sandboxclaims"

    def __init__(
        self,
        namespace: str = "default",
        template_name: str = "codemender-template",
        warmpool_name: str = "codemender-warmpool",
        container_name: str = "codemender-worker",
        claim_timeout_seconds: int = 120,
    ) -> None:
        self.namespace = namespace
        self.template_name = template_name
        self.warmpool_name = warmpool_name
        self.container_name = container_name
        self.claim_timeout_seconds = claim_timeout_seconds
        self._loaded = False

    def _ensure_k8s_client(self) -> None:
        if self._loaded:
            return
        from kubernetes import config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._loaded = True

    def create_claim(self, claim_name: str, scan_id: str) -> Dict[str, Any]:
        """Creates a v1beta1 SandboxClaim resource in Kubernetes."""
        self._ensure_k8s_client()
        from kubernetes import client

        custom_api = client.CustomObjectsApi()
        body = build_sandbox_claim_manifest(
            claim_name=claim_name,
            namespace=self.namespace,
            warmpool_name=self.warmpool_name,
            scan_id=scan_id,
        )
        return custom_api.create_namespaced_custom_object(
            group=self.GROUP,
            version=self.VERSION,
            namespace=self.namespace,
            plural=self.PLURAL,
            body=body,
        )

    def wait_for_bound_pod(self, claim_name: str, scan_id: str) -> str:
        """Waits for the SandboxClaim to bind to a running warm-pool pod and returns its name."""
        self._ensure_k8s_client()
        from kubernetes import client

        custom_api = client.CustomObjectsApi()
        core_api = client.CoreV1Api()
        deadline = time.time() + self.claim_timeout_seconds

        while time.time() < deadline:
            claim_obj = custom_api.get_namespaced_custom_object(
                group=self.GROUP,
                version=self.VERSION,
                namespace=self.namespace,
                plural=self.PLURAL,
                name=claim_name,
            )
            claim_meta = claim_obj.get("metadata", {}) or {}
            claim_annotations = claim_meta.get("annotations", {}) or {}
            status = claim_obj.get("status", {}) or {}
            sandbox_block = status.get("sandbox") or {}

            sandbox_name = (
                sandbox_block.get("name")
                or claim_annotations.get("agents.x-k8s.io/sandbox-name")
                or status.get("sandboxName")
                or status.get("podName")
                or (status.get("sandboxRef") or {}).get("name")
            )

            pods = core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector="sandbox-type=codemender-runtime",
            )
            for pod in pods.items:
                metadata = pod.metadata
                annotations = metadata.annotations or {}
                labels = metadata.labels or {}
                owner_refs = metadata.owner_references or []

                matches_claim = (
                    (sandbox_name and metadata.name == sandbox_name)
                    or labels.get("agents.x-k8s.io/claim-name") == claim_name
                    or labels.get("extensions.agents.x-k8s.io/claim-name") == claim_name
                    or annotations.get("agents.x-k8s.io/claim-name") == claim_name
                    or annotations.get("extensions.agents.x-k8s.io/claim-name") == claim_name
                    or any(ref.name == sandbox_name for ref in owner_refs if sandbox_name)
                )
                if matches_claim and pod.status.phase == "Running":
                    # Brief pause to allow gVisor warm-pool resume before exec
                    time.sleep(1.0)
                    return metadata.name

            time.sleep(1.5)

        raise TimeoutError(
            f"Timed out waiting for SandboxClaim '{claim_name}' (scan {scan_id}) to bind to a running pod."
        )

    def read_pod_status_json(self, pod_name: str, scan_id: str) -> Optional[Dict[str, Any]]:
        """Reads /workspace/out/<scan_id>/status.json from the bound pod if available."""
        self._ensure_k8s_client()
        from kubernetes import client
        from kubernetes.stream import stream

        core_api = client.CoreV1Api()
        try:
            raw_json = stream(
                core_api.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                container=self.container_name,
                command=["cat", f"/workspace/out/{scan_id}/status.json"],
                stderr=False,
                stdin=False,
                stdout=True,
                tty=False,
            )
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Could not read status.json from pod %s: %s", pod_name, exc)
        return None

    def exec_scan_in_pod(
        self,
        pod_name: str,
        job: ScanJob,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Executes /usr/local/bin/run_scan.sh inside the bound sandbox pod."""
        self._ensure_k8s_client()
        from kubernetes import client
        from kubernetes.stream import stream

        core_api = client.CoreV1Api()
        cmd = build_worker_command(job)
        ws_client = stream(
            core_api.connect_get_namespaced_pod_exec,
            pod_name,
            self.namespace,
            container=self.container_name,
            command=cmd,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )

        last_poll = time.time()
        while ws_client.is_open():
            ws_client.update(timeout=2)
            if ws_client.peek_stdout():
                ws_client.read_stdout()
            if ws_client.peek_stderr():
                ws_client.read_stderr()
            if on_progress and (time.time() - last_poll) >= 5.0:
                last_poll = time.time()
                live_status = self.read_pod_status_json(pod_name, job.scan_id)
                if live_status:
                    on_progress(live_status)

        ws_client.close()
        exit_code = ws_client.returncode or 0

        status_data: Dict[str, Any] = {"exit_code": exit_code}
        final_status = self.read_pod_status_json(pod_name, job.scan_id)
        if final_status:
            status_data.update(final_status)

        return status_data

    def delete_claim(self, claim_name: str) -> None:
        """Deletes the SandboxClaim to release the sandbox back to the pool."""
        if not claim_name:
            return
        self._ensure_k8s_client()
        from kubernetes import client

        custom_api = client.CustomObjectsApi()
        try:
            custom_api.delete_namespaced_custom_object(
                group=self.GROUP,
                version=self.VERSION,
                namespace=self.namespace,
                plural=self.PLURAL,
                name=claim_name,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to delete SandboxClaim %s: %s", claim_name, exc)


class SandboxOrchestrator:
    """Coordinates concurrent CodeMender scan jobs across the GKE Agent Sandbox pool."""

    def __init__(
        self,
        backend: Optional[Any] = None,
        out_bucket: str = "",
        run_async: bool = True,
        state_file: str = "",
    ) -> None:
        self.backend = backend or K8sSandboxBackend(
            namespace=os.environ.get("K8S_NAMESPACE", "default"),
            template_name=os.environ.get("SANDBOX_TEMPLATE_NAME", "codemender-template"),
            warmpool_name=os.environ.get("SANDBOX_WARMPOOL_NAME", "codemender-warmpool"),
        )
        self.out_bucket = out_bucket or os.environ.get("OUT_BUCKET", "")
        self.run_async = run_async
        self.state_file = Path(
            state_file or os.environ.get("ORCHESTRATOR_STATE_FILE", "/tmp/codemender_scans.json")
        )
        self._jobs: Dict[str, ScanJob] = {}
        self._lock = threading.Lock()
        self._load_state()

    def _load_state(self) -> None:
        """Loads persisted scan jobs from disk if present."""
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text())
            if isinstance(raw, dict):
                for scan_id, data in raw.items():
                    if isinstance(data, dict):
                        valid_keys = ScanJob.__dataclass_fields__.keys()
                        filtered = {k: v for k, v in data.items() if k in valid_keys}
                        self._jobs[scan_id] = ScanJob(**filtered)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Could not load state file %s: %s", self.state_file, exc)

    def _save_state_locked(self) -> None:
        """Persists current scan jobs to disk (must be called under self._lock)."""
        try:
            serialized = {
                scan_id: {
                    k: getattr(job, k) for k in ScanJob.__dataclass_fields__.keys()
                }
                for scan_id, job in self._jobs.items()
            }
            tmp_path = self.state_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(serialized, indent=2))
            tmp_path.replace(self.state_file)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Could not persist state file %s: %s", self.state_file, exc)

    def submit_scan(self, payload: Dict[str, Any]) -> ScanJob:
        """Validates parameters, creates a ScanJob, and dispatches execution."""
        params = validate_scan_request(payload)
        short_id = uuid.uuid4().hex[:8]
        scan_id = f"scan-{int(time.time())}-{short_id}"
        claim_name = f"cm-claim-{short_id}"

        job = ScanJob(
            scan_id=scan_id,
            target_path=params["target_path"],
            scan_mode=params["scan_mode"],
            model=params["model"],
            workers=params["workers"],
            diff_ref=params["diff_ref"],
            auto_release=params["auto_release"],
            claim_name=claim_name,
        )

        with self._lock:
            self._load_state()
            self._jobs[scan_id] = job
            self._save_state_locked()

        if self.run_async:
            worker_thread = threading.Thread(
                target=self._execute_job,
                args=(scan_id,),
                daemon=True,
            )
            worker_thread.start()
        else:
            self._execute_job(scan_id)

        return self.get_scan(scan_id) or job

    def _update_job(self, scan_id: str, **kwargs: Any) -> None:
        with self._lock:
            self._load_state()
            job = self._jobs.get(scan_id)
            if not job:
                return
            for key, value in kwargs.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save_state_locked()

    def _execute_job(self, scan_id: str) -> None:
        job = self.get_scan(scan_id)
        if not job:
            return

        try:
            self._update_job(
                scan_id,
                state="CLAIMING_SANDBOX",
                message=f"Creating SandboxClaim {job.claim_name} from warm pool",
            )
            self.backend.create_claim(job.claim_name, job.scan_id)

            pod_name = self.backend.wait_for_bound_pod(job.claim_name, job.scan_id)
            self._update_job(
                scan_id,
                pod_name=pod_name,
                state="RUNNING",
                message=f"Bound to warm sandbox pod {pod_name}; staging to local SSD and scanning",
            )

            def _progress_cb(live: Dict[str, Any]) -> None:
                self._update_job(
                    scan_id,
                    state=live.get("state", "RUNNING"),
                    message=live.get("message", f"Running on {pod_name}"),
                    staging_duration_seconds=int(live.get("staging_duration_seconds", 0)),
                    scan_duration_seconds=int(live.get("scan_duration_seconds", 0)),
                )

            try:
                result = self.backend.exec_scan_in_pod(pod_name, job, on_progress=_progress_cb)
            except TypeError:
                result = self.backend.exec_scan_in_pod(pod_name, job)

            exit_code = int(result.get("exit_code", 0))
            stage_sec = int(result.get("staging_duration_seconds", 0))
            scan_sec = int(result.get("scan_duration_seconds", 0))
            final_state = "COMPLETED" if exit_code == 0 else "FAILED"
            final_msg = result.get(
                "message",
                "Scan completed successfully" if exit_code == 0 else f"Scan exited with code {exit_code}",
            )

            self._update_job(
                scan_id,
                state=final_state,
                message=final_msg,
                staging_duration_seconds=stage_sec,
                scan_duration_seconds=scan_sec,
                exit_code=exit_code,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Scan job %s failed", scan_id)
            self._update_job(
                scan_id,
                state="FAILED",
                message=str(exc),
                exit_code=1,
            )
        finally:
            current = self.get_scan(scan_id)
            if current and current.auto_release and current.claim_name:
                self.backend.delete_claim(current.claim_name)

    def get_scan(self, scan_id: str) -> Optional[ScanJob]:
        with self._lock:
            self._load_state()
            return self._jobs.get(scan_id)

    def list_scans(self) -> List[ScanJob]:
        with self._lock:
            self._load_state()
            return sorted(
                self._jobs.values(),
                key=lambda j: j.created_at,
                reverse=True,
            )

    def cancel_or_delete_scan(self, scan_id: str) -> Optional[ScanJob]:
        with self._lock:
            self._load_state()
            job = self._jobs.get(scan_id)
        if not job:
            return None
        if job.claim_name:
            self.backend.delete_claim(job.claim_name)
        self._update_job(
            scan_id,
            state="CANCELED" if job.state not in ("COMPLETED", "FAILED") else job.state,
            message="SandboxClaim released",
        )
        return self.get_scan(scan_id)
