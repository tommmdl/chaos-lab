#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
K=${KUBECTL:-$ROOT/../kubernetes-lab/.local/bin/kubectl}
CONFIG=${KUBECONFIG_CHAOS:-$ROOT/../kubernetes-lab/.local/kubeconfig}
exec "$K" --kubeconfig "$CONFIG" --context kind-devops-notes --namespace chaos-demo "$@"
