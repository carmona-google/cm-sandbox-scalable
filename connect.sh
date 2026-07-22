#!/usr/bin/env bash
set -e

# Find the active sandbox pod created by the warmpool/claim
POD_NAME=$(kubectl get pods -l sandbox-type=codemender-runtime -o jsonpath='{.items[0].metadata.name}')

if [ -z "$POD_NAME" ]; then
  echo "Error: Could not find a running Codemender sandbox pod."
  echo "Check 'kubectl get pods' to ensure it is running."
  exit 1
fi

echo "Connecting to Codemender sandbox pod: $POD_NAME"
echo "You are now inside the secure gVisor sandbox."
echo ""
echo "Tips for using Codemender:"
echo " - Your source code bucket is mounted at: /workspace/src"
echo " - Your output reports bucket is mounted at: /workspace/out"
echo " - Codemender writes reports to: ~/.codemender/reports"
echo " - Example commands:"
echo "   cm find /workspace/src"
echo "   cm verify /workspace/src"
echo "   cm report"
echo "   cp -r ~/.codemender/reports/* /workspace/out/"
echo ""
kubectl exec -it $POD_NAME -- /bin/bash
