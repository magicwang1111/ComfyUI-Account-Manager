#!/usr/bin/env bash
set -euo pipefail

export APP_DIR=/root/autodl-tmp/ComfyUI
export CONDA_SH=/root/miniconda3/etc/profile.d/conda.sh
export CONDA_ENV=comfyui
export START_PORT=6006
export INSTANCE_COUNT=4
export GPU_COUNT=2
export GPU_WORKER_COUNT=2
export GPU_WORKER_INDICES=1,3

exec bash /root/autodl-tmp/ComfyUI/custom_nodes/ComfyUI-Account-Manager/manage_comfyui.sh "$@"
