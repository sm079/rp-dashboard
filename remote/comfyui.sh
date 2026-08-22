#!/usr/bin/env bash
# Pin ComfyUI to a release and install custom nodes, inside the template's venv.
# Args: <comfyui git ref> [custom node git URL ...]
# Takes effect on the next ComfyUI start (setup.sh restarts it).
set -euo pipefail

COMFY_DIR=/workspace/runpod-slim/ComfyUI
REF="$1"; shift

cd "$COMFY_DIR"
# shellcheck source=/dev/null
source .venv-cu128/bin/activate

current="$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)"
if [ "$current" != "$REF" ]; then
  echo "Updating ComfyUI $current -> $REF"
  # --force: the template's baked v0.30.0 tag differs from upstream's.
  git fetch --quiet --tags --force origin
  git -c advice.detachedHead=false checkout --quiet "$REF"
  python -m pip install -q -r requirements.txt
else
  echo "ComfyUI already at $REF"
fi

total=$#
i=0
for url in "$@"; do
  i=$((i + 1))
  name="$(basename "$url" .git)"
  dir="custom_nodes/$name"
  if [ -d "$dir" ]; then
    echo "[$i/$total] $name already installed"
    continue
  fi
  echo "[$i/$total] Installing $name"
  git clone --quiet --depth 1 "$url" "$dir"
  if [ -f "$dir/requirements.txt" ]; then
    python -m pip install -q -r "$dir/requirements.txt"
  fi
done
