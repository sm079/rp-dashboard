#!/usr/bin/env bash
# Restart the template's ComfyUI so that:
#  - inputs, outputs, temp files, workflows/settings, synced LoRAs and logs live in
#    one data folder, in RAM (/dev/shm) or on the pod's disk;
#  - it listens on localhost only, reachable through the SSH tunnel;
#  - it is restarted automatically if it exits (e.g. killed for running out of RAM).
# Args: <ram|disk> [extra ComfyUI command-line flags ...]
set -euo pipefail

COMFY_DIR=/workspace/runpod-slim/ComfyUI
ARGS_FILE=/workspace/runpod-slim/comfyui_args.txt
RAM_DATA=/dev/shm/comfy
DISK_DATA=/workspace/comfy-data
case "${1:-}" in
  ram) DATA=$RAM_DATA ;;
  disk) DATA=$DISK_DATA ;;
  *) echo "Usage: setup.sh <ram|disk> [ComfyUI flags ...]" >&2; exit 2 ;;
esac
shift
# A fixed path to the data folder, whichever one it is; sync.py works through it.
DATA_LINK=/root/comfy-data
PORT=8188
SUPERVISOR_PID="$DATA/supervisor.pid"

[ -d "$COMFY_DIR" ] || { echo "ComfyUI not found at $COMFY_DIR" >&2; exit 1; }
echo "$COMFY_DIR" > /root/.comfyui_dir

mkdir -p "$DATA"/{input,output,temp,user,tmp,loras}
chmod 700 "$DATA"
ln -sfn "$DATA" "$DATA_LINK"
echo "User data folder: $DATA"
# Seed the user dir with the template's default settings (no user data yet).
if [ -d "$COMFY_DIR/user" ] && [ -z "$(ls -A "$DATA/user")" ]; then
  cp -a "$COMFY_DIR/user/." "$DATA/user/"
fi

# LoRAs synced from the local sync folder go in the data folder as an extra LoRA folder.
cat > "$DATA/extra_model_paths.yaml" <<EOF
runpod_helper:
  base_path: $DATA
  loras: loras
EOF

# Later flags win, so these override the template's --listen 0.0.0.0.
# Written to the args file too, so a pod restart comes back the same way.
{
  echo "# Managed by runpod-helper: localhost only, user data in $DATA"
  echo "--listen 127.0.0.1"
  echo "--input-directory $DATA/input"
  echo "--output-directory $DATA/output"
  echo "--temp-directory $DATA/temp"
  echo "--user-directory $DATA/user"
  echo "--preview-method auto"
  echo "--extra-model-paths-config $DATA/extra_model_paths.yaml"
  for arg in "$@"; do echo "$arg"; done
} > "$ARGS_FILE"

# Services the SSH-only setup doesn't use.
pkill -x filebrowser 2>/dev/null || true
pkill -f jupyter-lab 2>/dev/null || true

# Stop our previous supervisor (from either data folder), then any ComfyUI
# (including the template's; start.sh keeps the container alive after it exits).
for pid_file in "$RAM_DATA/supervisor.pid" "$DISK_DATA/supervisor.pid"; do
  if [ -f "$pid_file" ]; then
    kill "$(cat "$pid_file")" 2>/dev/null || true
    rm -f "$pid_file"
  fi
done
if pkill -f "python main.py" 2>/dev/null; then
  for _ in $(seq 30); do pgrep -f "python main.py" >/dev/null || break; sleep 1; done
fi

cd "$COMFY_DIR"
# The venv name follows the template's CUDA version (.venv-cu128, .venv-cu130, ...).
VENV="$(ls -d .venv-cu* 2>/dev/null | grep -v '\.bak' | sort | tail -1)"
[ -n "$VENV" ] || { echo "No ComfyUI venv found in $COMFY_DIR" >&2; exit 1; }

cat > "$DATA/run-comfyui.sh" <<EOF
#!/usr/bin/env bash
cd "$COMFY_DIR"
source "$VENV/bin/activate"
# Scratch files from Python's tempfile, ffmpeg and custom nodes go to the data folder too.
export TMPDIR="$DATA/tmp"
mapfile -t EXTRA < <(grep -v '^[[:space:]]*#' "$ARGS_FILE" | tr -s '[:space:]' '\n' | sed '/^\$/d')
while :; do
  python main.py --port "$PORT" --enable-cors-header "\${EXTRA[@]}"
  echo "[runpod-helper] ComfyUI exited with code \$? at \$(date -u +%FT%TZ); restarting in 3s"
  sleep 3
done
EOF
# /dev/shm is mounted noexec, so run the script through bash.
setsid nohup bash "$DATA/run-comfyui.sh" > "$DATA/comfyui.log" 2>&1 < /dev/null &
echo $! > "$SUPERVISOR_PID"

echo -n "Starting ComfyUI"
for i in $(seq 180); do
  if curl -fs "http://127.0.0.1:$PORT/system_stats" >/dev/null; then
    echo " ready after ${i}s"
    break
  fi
  if ! kill -0 "$(cat "$SUPERVISOR_PID")" 2>/dev/null; then
    echo; echo "ComfyUI supervisor exited; last log lines:" >&2; tail -30 "$DATA/comfyui.log" >&2; exit 1
  fi
  echo -n "."; sleep 1
done

# cgroup v2, then v1.
limit="$(cat /sys/fs/cgroup/memory.max 2>/dev/null \
  || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo unknown)"
case "$limit" in
  ''|*[!0-9]*) echo "Container RAM limit: $limit" ;;
  *) echo "Container RAM limit: $(( limit / 1024 / 1024 / 1024 )) GB" ;;
esac
df -h "$DATA/" | tail -1
