"""Flask REST API for orchestrating scalable CodeMender scans on GKE Agent Sandbox."""

import os
from typing import Any, Optional
from flask import Flask, jsonify, request

from orchestrator.sandbox_manager import SandboxOrchestrator


def create_app(orchestrator: Optional[SandboxOrchestrator] = None) -> Flask:
    """Application factory for the CodeMender Sandbox Orchestrator API."""
    app = Flask(__name__)
    app.config["ORCHESTRATOR"] = orchestrator or SandboxOrchestrator()

    def get_orchestrator() -> SandboxOrchestrator:
        return app.config["ORCHESTRATOR"]

    @app.route("/healthz", methods=["GET"])
    def healthz() -> Any:
        return jsonify({
            "status": "ok",
            "service": "codemender-sandbox-orchestrator",
        }), 200

    @app.route("/api/v1/scans", methods=["POST"])
    def create_scan() -> Any:
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "Invalid or missing JSON request body."}), 400

        orch = get_orchestrator()
        try:
            job = orch.submit_scan(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(job.to_dict(out_bucket=orch.out_bucket)), 202

    @app.route("/api/v1/scans", methods=["GET"])
    def list_scans() -> Any:
        orch = get_orchestrator()
        scans = [job.to_dict(out_bucket=orch.out_bucket) for job in orch.list_scans()]
        return jsonify({"scans": scans, "count": len(scans)}), 200

    @app.route("/api/v1/scans/<scan_id>", methods=["GET"])
    def get_scan(scan_id: str) -> Any:
        orch = get_orchestrator()
        job = orch.get_scan(scan_id)
        if not job:
            return jsonify({"error": f"Scan '{scan_id}' not found."}), 404
        return jsonify(job.to_dict(out_bucket=orch.out_bucket)), 200

    @app.route("/api/v1/scans/<scan_id>", methods=["DELETE"])
    def delete_scan(scan_id: str) -> Any:
        orch = get_orchestrator()
        job = orch.cancel_or_delete_scan(scan_id)
        if not job:
            return jsonify({"error": f"Scan '{scan_id}' not found."}), 404
        return jsonify(job.to_dict(out_bucket=orch.out_bucket)), 200

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    flask_app = create_app()
    flask_app.run(host="0.0.0.0", port=port)
