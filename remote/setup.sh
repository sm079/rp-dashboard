#!/usr/bin/env bash
# Restart the template's ComfyUI so that:
#  - inputs, outputs, temp files, workflows/settings and logs live in RAM
#    (/dev/shm);
#  - it listens on localhost only, reachable through the SSH tunnel;
#  - it is restarted automatically if it exits (e.g. killed for running out of RAM).
# Args: extra ComfyUI command-line flags.
set -euo pipefail

COMFY_DIR=/workspace/runpod-slim/ComfyUI
ARGS_FILE=/workspace/runpod-slim/comfyui_args.txt
RAM=/dev/shm/comfy
PORT=8188
SUPERVISOR_PID="$RAM/supervisor.pid"

[ -d "$COMFY_DIR" ] || { echo "ComfyUI not found at $COMFY_DIR" >&2; exit 1; }
echo "$COMFY_DIR" > /root/.comfyui_dir

mkdir -p "$RAM"/{input,output,temp,user,tmp}
chmod 700 "$RAM"
# Seed the RAM user dir with the template's default settings (no user data yet).
if [ -d "$COMFY_DIR/user" ] && [ -z "$(ls -A "$RAM/user")" ]; then
  cp -a "$COMFY_DIR/user/." "$RAM/user/"
fi

# Later flags win, so these override the template's --listen 0.0.0.0.
# Written to the args file too, so a pod restart comes back the same way.
{
  echo "# Managed by runpod-helper: localhost only, user folders in RAM"
  echo "--listen 127.0.0.1"
  echo "--input-directory $RAM/input"
  echo "--output-directory $RAM/output"
  echo "--temp-directory $RAM/temp"
  echo "--user-directory $RAM/user"
  echo "--preview-method auto"
  for arg in "$@"; do echo "$arg"; done
} > "$ARGS_FILE"

# Services the SSH-only setup doesn't use.
pkill -x filebrowser 2>/dev/null || true
pkill -f jupyter-lab 2>/dev/null || true

# Stop our previous supervisor, then any ComfyUI (including the template's;
# start.sh keeps the container alive after it exits).
if [ -f "$SUPERVISOR_PID" ]; then
  kill "$(cat "$SUPERVISOR_PID")" 2>/dev/null || true
  rm -f "$SUPERVISOR_PID"
fi
if pkill -f "python main.py" 2>/dev/null; then
  for _ in $(seq 30); do pgrep -f "python main.py" >/dev/null || break; sleep 1; done
fi

cd "$COMFY_DIR"
# The venv name follows the template's CUDA version (.venv-cu128, .venv-cu130, ...).
VENV="$(ls -d .venv-cu* 2>/dev/null | grep -v '\.bak' | sort | tail -1)"
[ -n "$VENV" ] || { echo "No ComfyUI venv found in $COMFY_DIR" >&2; exit 1; }

cat > "$RAM/run-comfyui.sh" <<EOF
#!/usr/bin/env bash
cd "$COMFY_DIR"
source "$VENV/bin/activate"
# Scratch files from Python's tempfile, ffmpeg and custom nodes go to RAM too.
export TMPDIR="$RAM/tmp"
mapfile -t EXTRA < <(grep -v '^[[:space:]]*#' "$ARGS_FILE" | tr -s '[:space:]' '\n' | sed '/^\$/d')
while :; do
  python main.py --port "$PORT" --enable-cors-header "\${EXTRA[@]}"
  echo "[runpod-helper] ComfyUI exited with code \$? at \$(date -u +%FT%TZ); restarting in 3s"
  sleep 3
done
EOF
# /dev/shm is mounted noexec, so run the script through bash.
setsid nohup bash "$RAM/run-comfyui.sh" > "$RAM/comfyui.log" 2>&1 < /dev/null &
echo $! > "$SUPERVISOR_PID"

echo -n "Starting ComfyUI"
for i in $(seq 180); do
  if curl -fs "http://127.0.0.1:$PORT/system_stats" >/dev/null; then
    echo " ready after ${i}s"
    break
  fi
  if ! kill -0 "$(cat "$SUPERVISOR_PID")" 2>/dev/null; then
    echo; echo "ComfyUI supervisor exited; last log lines:" >&2; tail -30 "$RAM/comfyui.log" >&2; exit 1
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
df -h /dev/shm | tail -1
