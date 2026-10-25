#!/usr/bin/env bash
# Headless worker script executed inside a claimed gVisor sandbox pod.
# Stages code from GCS FUSE (/workspace/src) to node-local ephemeral SSD
# (/workspace/local-work) to eliminate FUSE I/O latency during scanning,
# runs CodeMender, and exports reports to GCS (/workspace/out).
set -euo pipefail

SCAN_ID="${SCAN_ID:-scan-$(date +%s)}"
TARGET_SUBPATH="${TARGET_SUBPATH:-.}"
SCAN_MODE="${SCAN_MODE:-quick}"       # quick | deep | diff
MODEL="${MODEL:-}"
WORKERS="${WORKERS:-4}"
DIFF_REF="${DIFF_REF:-HEAD}"
CLEANUP_LOCAL="${CLEANUP_LOCAL:-true}"

SRC_ROOT="/workspace/src"
OUT_ROOT="/workspace/out"
LOCAL_WORK_ROOT="/workspace/local-work"

# Sanitize TARGET_SUBPATH to prevent path traversal outside /workspace/src
CLEAN_SUBPATH=$(python3 -c "import os, sys; p = os.path.normpath(sys.argv[1]).lstrip('/'); print('' if p.startswith('..') else p)" "${TARGET_SUBPATH}")
SRC_PATH="${SRC_ROOT}/${CLEAN_SUBPATH}"

SCAN_WORK_DIR="${LOCAL_WORK_ROOT}/${SCAN_ID}"
SCAN_SRC_DIR="${SCAN_WORK_DIR}/repo"
export CM_HOME="${SCAN_WORK_DIR}/.codemender"
SCAN_OUT_DIR="${OUT_ROOT}/${SCAN_ID}"

mkdir -p "${SCAN_SRC_DIR}" "${CM_HOME}" "${SCAN_OUT_DIR}"

STATUS_FILE="${SCAN_OUT_DIR}/status.json"
LOG_FILE="${SCAN_OUT_DIR}/scan.log"

write_status() {
  local state="$1"
  local msg="$2"
  local stage_sec="${3:-0}"
  local scan_sec="${4:-0}"
  local exit_code="${5:-0}"
  cat <<JSON > "${STATUS_FILE}"
{
  "scan_id": "${SCAN_ID}",
  "target_subpath": "${CLEAN_SUBPATH}",
  "scan_mode": "${SCAN_MODE}",
  "model": "${MODEL}",
  "workers": ${WORKERS},
  "state": "${state}",
  "message": "${msg}",
  "staging_duration_seconds": ${stage_sec},
  "scan_duration_seconds": ${scan_sec},
  "exit_code": ${exit_code},
  "updated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
JSON
}

exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting scan ${SCAN_ID} (mode=${SCAN_MODE}, target=${CLEAN_SUBPATH})"

if [ ! -e "${SRC_PATH}" ]; then
  write_status "FAILED" "Source path ${SRC_PATH} does not exist in GCS bucket" 0 0 1
  echo "ERROR: Source path ${SRC_PATH} not found."
  exit 1
fi

# ---------------------------------------------------------
# Stage 1: Fast Local Copy from GCS FUSE to Local emptyDir
# ---------------------------------------------------------
write_status "STAGING" "Copying source code from GCS FUSE to local ephemeral SSD (emptyDir)" 0 0 0
STAGE_START=$(date +%s)

cp -a "${SRC_PATH}/." "${SCAN_SRC_DIR}/"

STAGE_END=$(date +%s)
STAGE_DURATION=$((STAGE_END - STAGE_START))
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Staged source code to ${SCAN_SRC_DIR} in ${STAGE_DURATION}s"

# Initialize isolated CodeMender workspace on local SSD
cm init
if [ -f "/tmp/config/config.yaml" ]; then
  cp /tmp/config/config.yaml "${CM_HOME}/config.yaml"
fi

# ---------------------------------------------------------
# Stage 2: Execute CodeMender Scan against Local Storage
# ---------------------------------------------------------
write_status "SCANNING" "Running CodeMender (${SCAN_MODE}) on local ephemeral storage" "${STAGE_DURATION}" 0 0
SCAN_START=$(date +%s)

CM_ARGS=("find" "--compact" "-y")
if [ -n "${MODEL}" ]; then
  CM_ARGS+=("--model" "${MODEL}")
fi

case "${SCAN_MODE}" in
  deep)
    CM_ARGS+=("--deep" "--deep-workers" "${WORKERS}")
    ;;
  diff)
    CM_ARGS+=("--diff=${DIFF_REF}" "--diff-workers" "${WORKERS}")
    ;;
  quick|*)
    ;;
esac

CM_ARGS+=("${SCAN_SRC_DIR}")

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Executing: cm ${CM_ARGS[*]}"
set +e
cm "${CM_ARGS[@]}"
CM_EXIT=$?
set -e

SCAN_END=$(date +%s)
SCAN_DURATION=$((SCAN_END - SCAN_START))
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Scan finished in ${SCAN_DURATION}s (exit_code=${CM_EXIT})"

# ---------------------------------------------------------
# Stage 3: Export Reports to GCS Output Bucket
# ---------------------------------------------------------
write_status "EXPORTING" "Exporting reports to GCS output bucket" "${STAGE_DURATION}" "${SCAN_DURATION}" "${CM_EXIT}"

set +e
cm report --format json > "${SCAN_OUT_DIR}/findings.json" 2>/dev/null
cm report --format sarif > "${SCAN_OUT_DIR}/findings.sarif" 2>/dev/null
cm report --format html > "${SCAN_OUT_DIR}/report.html" 2>/dev/null
cm report --format md > "${SCAN_OUT_DIR}/report.md" 2>/dev/null
if [ -d "${CM_HOME}/reports" ]; then
  cp -r "${CM_HOME}/reports/." "${SCAN_OUT_DIR}/" 2>/dev/null || true
fi
set -e

if [ "${CLEANUP_LOCAL}" = "true" ]; then
  rm -rf "${SCAN_WORK_DIR}"
fi

if [ "${CM_EXIT}" -eq 0 ]; then
  write_status "COMPLETED" "Scan completed successfully" "${STAGE_DURATION}" "${SCAN_DURATION}" 0
else
  write_status "FAILED" "CodeMender exited with code ${CM_EXIT}" "${STAGE_DURATION}" "${SCAN_DURATION}" "${CM_EXIT}"
fi

exit "${CM_EXIT}"
