#!/usr/bin/env bash
# Download model files from Hugging Face into ComfyUI's models folder.
# Args: one or more "repo::path1,path2,..." specs. Repo paths mirror ComfyUI's
# models/ layout (diffusion_models/, text_encoders/, vae/, loras/), so files
# are downloaded straight into place.
set -euo pipefail

COMFY_DIR="$(cat /root/.comfyui_dir 2>/dev/null || true)"
if [ -z "$COMFY_DIR" ] || [ ! -d "$COMFY_DIR/models" ]; then
  echo "ComfyUI dir unknown; run setup first" >&2
  exit 1
fi

if ! command -v hf >/dev/null 2>&1; then
  echo "Installing huggingface_hub CLI..."
  pip install -q -U "huggingface_hub[hf_xet]"
fi
export HF_XET_HIGH_PERFORMANCE=1

total=$#
i=0
for spec in "$@"; do
  i=$((i + 1))
  repo="${spec%%::*}"
  IFS=',' read -r -a files <<< "${spec#*::}"
  echo "[$i/$total] $repo (${#files[@]} files)"
  hf download "$repo" "${files[@]}" --local-dir "$COMFY_DIR/models"
done

echo "Models ready in $COMFY_DIR/models"
df -h "$COMFY_DIR" | tail -1
