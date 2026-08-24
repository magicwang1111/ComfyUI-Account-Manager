#!/usr/bin/env bash

set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-/root/autodl-tmp/ComfyUI}"
CONDA_ENV="${CONDA_ENV:-comfyui}"
HOST="${HOST:-0.0.0.0}"
START_PORT="${START_PORT:-6006}"
INSTANCE_COUNT="${INSTANCE_COUNT:-42}"
GPU_COUNT="${GPU_COUNT:-2}"
GPU_WORKER_COUNT="${GPU_WORKER_COUNT:-2}"
GPU_WORKER_INDICES="${GPU_WORKER_INDICES:-1,3}"
SESSION_PREFIX="${SESSION_PREFIX:-comfyui}"
PYTHON_BIN="${PYTHON_BIN:-python}"
STOP_TIMEOUT="${STOP_TIMEOUT:-10}"
ACCOUNT_MANAGER_DIR="${ACCOUNT_MANAGER_DIR:-${APP_DIR}/custom_nodes/ComfyUI-Account-Manager}"
INSTANCE_LOG_DIR="${INSTANCE_LOG_DIR:-${ACCOUNT_MANAGER_DIR}/logs/instances}"

usage() {
  cat <<'EOF'
Usage:
  ./manage_comfyui.sh start [all|TARGET...]
  ./manage_comfyui.sh stop [all|TARGET...]
  ./manage_comfyui.sh restart [all|TARGET...]
  ./manage_comfyui.sh status [all|TARGET...]
  ./manage_comfyui.sh attach TARGET
  ./manage_comfyui.sh logs [-f|--follow] TARGET
  ./manage_comfyui.sh job-logs [-f|--follow|--status] JOB_ID

TARGET can be:
  - instance index: 1, 2, 3...
  - port: 6006, 6007, 6008...
  - tmux session: comfyui, comfyui2, comfyui3...

Examples:
  ./manage_comfyui.sh start
  ./manage_comfyui.sh restart 1 3 6010 comfyui7
  ./manage_comfyui.sh stop all
  ./manage_comfyui.sh status
  ./manage_comfyui.sh attach 6006
  ./manage_comfyui.sh logs comfyui4
  ./manage_comfyui.sh logs --follow 6006
  ./manage_comfyui.sh job-logs cc8f0bef-6783-4367-a7bb-776c6f90ec8d

Defaults:
  APP_DIR=/root/autodl-tmp/ComfyUI
  CONDA_ENV=comfyui
  START_PORT=6006
  INSTANCE_COUNT=42
  GPU_COUNT=2
  GPU_WORKER_COUNT=2
  GPU_WORKER_INDICES=1,3
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

detect_conda_sh() {
  if [[ -n "${CONDA_SH:-}" && -f "${CONDA_SH}" ]]; then
    echo "${CONDA_SH}"
    return 0
  fi

  if command -v conda >/dev/null 2>&1; then
    local conda_base
    conda_base="$(conda info --base 2>/dev/null || true)"
    if [[ -n "${conda_base}" && -f "${conda_base}/etc/profile.d/conda.sh" ]]; then
      echo "${conda_base}/etc/profile.d/conda.sh"
      return 0
    fi
  fi

  local candidates=(
    "$HOME/miniconda3/etc/profile.d/conda.sh"
    "$HOME/anaconda3/etc/profile.d/conda.sh"
    "/opt/conda/etc/profile.d/conda.sh"
    "/usr/local/miniconda3/etc/profile.d/conda.sh"
    "/usr/local/anaconda3/etc/profile.d/conda.sh"
  )

  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -f "${candidate}" ]]; then
      echo "${candidate}"
      return 0
    fi
  done

  return 1
}

session_name_for_index() {
  local index="$1"
  if [[ "${index}" -eq 1 ]]; then
    echo "${SESSION_PREFIX}"
  else
    echo "${SESSION_PREFIX}${index}"
  fi
}

port_for_index() {
  local index="$1"
  echo $((START_PORT + index - 1))
}

session_exists() {
  local session="$1"
  tmux has-session -t "${session}" 2>/dev/null
}

port_in_use() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    ss -ltnH | awk -v p=":${port}" '$4 ~ p"$" { found=1 } END { exit !found }'
    return $?
  fi

  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi

  return 1
}

# 强制结束占用指定端口的进程
kill_port() {
  local port="$1"

  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" 2>/dev/null || true
    return 0
  fi

  if command -v lsof >/dev/null 2>&1; then
    lsof -ti TCP:"${port}" -sTCP:LISTEN 2>/dev/null | xargs -r kill -9 2>/dev/null || true
    return 0
  fi

  if command -v ss >/dev/null 2>&1; then
    ss -tlnpH | awk -v p=":${port}" '
      $4 ~ p"$" {
        while (match($0, /pid=([0-9]+)/, a)) {
          print a[1]
          $0 = substr($0, RSTART + RLENGTH)
        }
      }
    ' | xargs -r kill -9 2>/dev/null || true
  fi
}

build_launch_command() {
  local port="$1"
  local index="$2"
  local log_file="$3"
  local worker_class
  local cuda_device=""
  local python_cmd
  local database_url="sqlite:///${APP_DIR}/user/comfyui-${port}.db"
  local -a command=(
    "${PYTHON_BIN}"
    main.py
    --enable-assets
    --enable-manager
    --database-url "${database_url}"
    --listen "${HOST}"
    --port "${port}"
    --disable-metadata
  )

  if cuda_device="$(gpu_device_for_index "${index}")"; then
    worker_class="gpu"
  else
    worker_class="api"
    command+=(--cpu)
  fi

  printf -v python_cmd '%q ' "${command[@]}"

  printf 'source %q && conda activate %q && cd %q && export CUDA_VISIBLE_DEVICES=%q && export ACCOUNT_MANAGER_WORKER_CLASS=%q && export ACCOUNT_MANAGER_INSTANCE_PORT=%q && export ACCOUNT_MANAGER_INSTANCE_LOG=%q && export PYTHONUNBUFFERED=1 && set -o pipefail && %s 2>&1 | tee -a %q' \
    "${CONDA_SH}" \
    "${CONDA_ENV}" \
    "${APP_DIR}" \
    "${cuda_device}" \
    "${worker_class}" \
    "${port}" \
    "${log_file}" \
    "${python_cmd}" \
    "${log_file}"
}

gpu_device_for_index() {
  local index="$1"
  local position=0
  local configured_index
  local -a gpu_indices
  IFS=',' read -r -a gpu_indices <<< "${GPU_WORKER_INDICES}"
  for configured_index in "${gpu_indices[@]}"; do
    if ((configured_index == index)); then
      echo $((position % GPU_COUNT))
      return 0
    fi
    position=$((position + 1))
  done
  return 1
}

worker_class_for_index() {
  local index="$1"
  local gpu_device
  if gpu_device="$(gpu_device_for_index "${index}")"; then
    echo "gpu:${gpu_device}"
  else
    echo "api:-"
  fi
}

index_for_target() {
  local target="$1"
  local index

  if [[ "${target}" =~ ^[0-9]+$ ]]; then
    if ((target >= 1 && target <= INSTANCE_COUNT)); then
      echo "${target}"
      return 0
    fi

    if ((target >= START_PORT && target <= 65535)); then
      echo $((target - START_PORT + 1))
      return 0
    fi
  fi

  for index in $(seq 1 "${INSTANCE_COUNT}"); do
    if [[ "$(session_name_for_index "${index}")" == "${target}" ]]; then
      echo "${index}"
      return 0
    fi
  done

  return 1
}

expand_targets() {
  if [[ "$#" -eq 0 ]]; then
    seq 1 "${INSTANCE_COUNT}"
    return 0
  fi

  local target
  local index
  declare -A seen=()

  for target in "$@"; do
    if [[ "${target}" == "all" ]]; then
      seq 1 "${INSTANCE_COUNT}"
      return 0
    fi

    index="$(index_for_target "${target}")" || die "unknown target: ${target}"
    seen["${index}"]=1
  done

  printf '%s\n' "${!seen[@]}" | sort -n
}

start_one() {
  local index="$1"
  local session
  local port
  local inner_cmd
  local escaped_cmd
  local log_file

  session="$(session_name_for_index "${index}")"
  port="$(port_for_index "${index}")"

  if session_exists "${session}"; then
    echo "[${session}] already running on port ${port}"
    return 0
  fi

  if port_in_use "${port}"; then
    echo "[${session}] port ${port} is already in use, skipped"
    return 0
  fi

  mkdir -p "${INSTANCE_LOG_DIR}/${port}"
  log_file="${INSTANCE_LOG_DIR}/${port}/$(date -u +%Y%m%dT%H%M%S%NZ).log"
  inner_cmd="$(build_launch_command "${port}" "${index}" "${log_file}")"
  printf -v escaped_cmd '%q' "${inner_cmd}"

  tmux new-session -d -s "${session}" -c "${APP_DIR}" "bash -lc ${escaped_cmd}"
  echo "[${session}] started on port ${port} ($(worker_class_for_index "${index}"))"
}

stop_one() {
  local index="$1"
  local session
  local port
  local second

  session="$(session_name_for_index "${index}")"
  port="$(port_for_index "${index}")"

  if ! session_exists "${session}"; then
    echo "[${session}] already stopped"
    return 0
  fi

  tmux send-keys -t "${session}" C-c 2>/dev/null || true

  for ((second = 0; second < STOP_TIMEOUT; second++)); do
    if ! session_exists "${session}"; then
      echo "[${session}] stopped"
      return 0
    fi
    sleep 1
  done

  tmux kill-session -t "${session}" 2>/dev/null || true
  echo "[${session}] stopped after force kill"
  return 0
}

restart_one() {
  local index="$1"
  local session
  local port
  local second

  session="$(session_name_for_index "${index}")"
  port="$(port_for_index "${index}")"

  # 第一步：强制结束 tmux session
  if session_exists "${session}"; then
    tmux kill-session -t "${session}" 2>/dev/null || true
  fi

  # 第二步：强制结束任何占用该端口的进程（无论是否由 tmux 管理）
  if port_in_use "${port}"; then
    kill_port "${port}"
  fi

  # 第三步：等待端口释放
  for ((second = 0; second < STOP_TIMEOUT; second++)); do
    if ! port_in_use "${port}"; then
      break
    fi
    sleep 1
  done

  if port_in_use "${port}"; then
    echo "[${session}] warning: port ${port} still in use after ${STOP_TIMEOUT}s, skipped"
    return 0
  fi

  # 第四步：启动
  start_one "${index}"
}

status_one() {
  local index="$1"
  local session
  local port
  local state
  local pid
  local worker

  session="$(session_name_for_index "${index}")"
  port="$(port_for_index "${index}")"
  worker="$(worker_class_for_index "${index}")"

  if session_exists "${session}"; then
    state="running"
    pid="$(tmux list-panes -t "${session}" -F '#{pane_pid}' 2>/dev/null | head -n 1 || true)"
  else
    state="stopped"
    pid="-"
  fi

  printf '%-10s %-6s %-8s %-8s %s\n' "${session}" "${port}" "${state}" "${worker}" "${pid}"
}

attach_one() {
  local index="$1"
  local session

  session="$(session_name_for_index "${index}")"
  session_exists "${session}" || die "session not running: ${session}"
  exec tmux attach -t "${session}"
}

latest_log_for_index() {
  local index="$1"
  local port
  local log_file

  port="$(port_for_index "${index}")"
  log_file="$(find "${INSTANCE_LOG_DIR}/${port}" -maxdepth 1 -type f -name '*.log' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-)"
  [[ -n "${log_file}" ]] || return 1
  echo "${log_file}"
}

logs_one() {
  local index="$1"
  local follow="${2:-false}"
  local log_file

  log_file="$(latest_log_for_index "${index}")" || die "no saved log for target: ${index}"
  if [[ "${follow}" == "true" ]]; then
    tail -n 200 -F "${log_file}"
  else
    tail -n 200 "${log_file}"
  fi
}

main() {
  local -a configured_gpu_indices
  local configured_gpu_index

  require_cmd tmux
  require_cmd bash
  require_cmd tee

  [[ "${GPU_COUNT}" =~ ^[1-9][0-9]*$ ]] || die "GPU_COUNT must be a positive integer"
  [[ "${GPU_WORKER_COUNT}" =~ ^[0-9]+$ ]] || die "GPU_WORKER_COUNT must be a non-negative integer"
  ((GPU_WORKER_COUNT <= INSTANCE_COUNT)) || die "GPU_WORKER_COUNT cannot exceed INSTANCE_COUNT"
  [[ "${GPU_WORKER_INDICES}" =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]] || die "GPU_WORKER_INDICES must be comma-separated instance indices"
  IFS=',' read -r -a configured_gpu_indices <<< "${GPU_WORKER_INDICES}"
  ((${#configured_gpu_indices[@]} == GPU_WORKER_COUNT)) || die "GPU_WORKER_COUNT must match GPU_WORKER_INDICES"
  for configured_gpu_index in "${configured_gpu_indices[@]}"; do
    ((configured_gpu_index <= INSTANCE_COUNT)) || die "GPU worker index exceeds INSTANCE_COUNT: ${configured_gpu_index}"
  done

  [[ -d "${APP_DIR}" ]] || die "APP_DIR does not exist: ${APP_DIR}"
  CONDA_SH="$(detect_conda_sh)" || die "could not locate conda.sh; set CONDA_SH manually if needed"
  readonly CONDA_SH

  local action="${1:-status}"
  shift || true

  case "${action}" in
    start|stop|restart|status)
      local -a targets
      mapfile -t targets < <(expand_targets "$@")

      if [[ "${action}" == "status" ]]; then
        printf '%-10s %-6s %-8s %-8s %s\n' "SESSION" "PORT" "STATE" "POOL:GPU" "PID"
      fi

      local index
      for index in "${targets[@]}"; do
        case "${action}" in
          start)   start_one   "${index}" || true ;;
          stop)    stop_one    "${index}" || true ;;
          restart) restart_one "${index}" || true ;;
          status)  status_one  "${index}" ;;
        esac
      done
      ;;
    attach)
      [[ "$#" -eq 1 ]] || die "attach requires exactly one TARGET"
      local index
      index="$(index_for_target "$1")" || die "unknown target: $1"
      attach_one "${index}"
      ;;
    logs)
      local follow="false"
      if [[ "${1:-}" == "-f" || "${1:-}" == "--follow" ]]; then
        follow="true"
        shift
      fi
      [[ "$#" -eq 1 ]] || die "logs requires exactly one TARGET"
      local index
      index="$(index_for_target "$1")" || die "unknown target: $1"
      logs_one "${index}" "${follow}"
      ;;
    job-logs)
      [[ "$#" -ge 1 ]] || die "job-logs requires a JOB_ID"
      source "${CONDA_SH}"
      conda activate "${CONDA_ENV}"
      "${PYTHON_BIN}" "${ACCOUNT_MANAGER_DIR}/admin/job_logs.py" "$@"
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      usage
      die "unknown action: ${action}"
      ;;
  esac
}

main "$@"
