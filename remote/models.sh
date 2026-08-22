#!/usr/bin/env bash
# Download model files from Hugging Face into ComfyUI's models folder.
# Args: one or more "repo::subdir::path1,path2,..." specs. Files go into
# models/<subdir>; with an empty subdir the repo paths are expected to mirror
# ComfyUI's models/ layout (diffusion_models/, text_encoders/, vae/, loras/).
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
  rest="${spec#*::}"
  subdir="${rest%%::*}"
  IFS=',' read -r -a files <<< "${rest#*::}"
  dest="$COMFY_DIR/models${subdir:+/$subdir}"
  echo "[$i/$total] $repo (${#files[@]} files) -> $dest"
  hf download "$repo" "${files[@]}" --local-dir "$dest"
done

echo "Models ready in $COMFY_DIR/models"
df -h "$COMFY_DIR" | tail -1
