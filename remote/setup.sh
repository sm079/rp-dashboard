#!/usr/bin/env bash
# Restart the template's ComfyUI so that:
#  - inputs, outputs, temp files, workflows/settings and logs live in RAM
#    (/dev/shm);
#  - it listens on localhost only, reachable through the SSH tunnel.
set -euo pipefail

COMFY_DIR=/workspace/runpod-slim/ComfyUI
ARGS_FILE=/workspace/runpod-slim/comfyui_args.txt
RAM=/dev/shm/comfy
PORT=8188

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
cat > "$ARGS_FILE" <<EOF
# Managed by runpod-helper: localhost only, user folders in RAM
--listen 127.0.0.1
--input-directory $RAM/input
--output-directory $RAM/output
--temp-directory $RAM/temp
--user-directory $RAM/user
--preview-method auto
EOF

# Services the SSH-only setup doesn't use.
pkill -x filebrowser 2>/dev/null || true
pkill -f jupyter-lab 2>/dev/null || true

# Stop the template's ComfyUI (start.sh keeps the container alive after it exits).
if pkill -f "python main.py" 2>/dev/null; then
  for _ in $(seq 30); do pgrep -f "python main.py" >/dev/null || break; sleep 1; done
fi

cd "$COMFY_DIR"
# shellcheck source=/dev/null
source .venv-cu128/bin/activate
# Scratch files from Python's tempfile, ffmpeg and custom nodes go to RAM too.
export TMPDIR="$RAM/tmp"
mapfile -t EXTRA < <(grep -v '^[[:space:]]*#' "$ARGS_FILE" | tr -s '[:space:]' '\n' | sed '/^$/d')
setsid nohup python main.py --port "$PORT" --enable-cors-header "${EXTRA[@]}" \
  > "$RAM/comfyui.log" 2>&1 < /dev/null &

echo -n "Starting ComfyUI"
for i in $(seq 180); do
  if curl -fs "http://127.0.0.1:$PORT/system_stats" >/dev/null; then
    echo " ready after ${i}s"
    break
  fi
  if ! pgrep -f "python main.py" >/dev/null; then
    echo; echo "ComfyUI exited; last log lines:" >&2; tail -30 "$RAM/comfyui.log" >&2; exit 1
  fi
  echo -n "."; sleep 1
done

echo "Listening sockets on $PORT:"
ss -ltnp 2>/dev/null | grep ":$PORT " || true
df -h /dev/shm | tail -1
