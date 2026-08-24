#!/usr/bin/env bash
set -euo pipefail

export APP_DIR=/mnt/ComfyUI
export CONDA_SH=/mnt/miniconda/etc/profile.d/conda.sh
export CONDA_ENV=comfyui
export START_PORT=8180
export INSTANCE_COUNT=9
export GPU_COUNT=1
export GPU_WORKER_COUNT=0
export GPU_WORKER_INDICES=""

exec bash /mnt/ComfyUI/custom_nodes/ComfyUI-Account-Manager/manage_comfyui.sh "$@"
