#!/usr/bin/env bash
# Download one LoRA into ComfyUI's models/loras/<subdir>/ folder.
# Args: <subdir> <file name> <url> <auth: civitai|hf|none>
# CIVITAI_TOKEN / HF_TOKEN are set at the top of the piped script (not in argv,
# not exported) and sent only to the link's own host: curl drops the header when
# it follows the redirect to the file storage.
set -euo pipefail

COMFY_DIR="$(cat /root/.comfyui_dir 2>/dev/null || true)"
if [ -z "$COMFY_DIR" ] || [ ! -d "$COMFY_DIR/models" ]; then
  echo "ComfyUI dir unknown; run setup first" >&2
  exit 1
fi

subdir="$1" file="$2" url="$3" auth="$4"
dest="$COMFY_DIR/models/loras/$subdir"
if [ -s "$dest/$file" ]; then
  echo "$subdir/$file is already on the pod"
  exit 0
fi
mkdir -p "$dest"
# A unique hidden temp name: two downloads of the same file can't clash, and
# ComfyUI never lists a half-written LoRA.
tmp="$(mktemp "$dest/.$file.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

case "$auth" in
  civitai) token="${CIVITAI_TOKEN:-}" ;;
  hf) token="${HF_TOKEN:-}" ;;
  *) token="" ;;
esac
args=(-fL --retry 3 --progress-bar -A runpod-helper/0.1 -o "$tmp")
if [ -n "$token" ]; then
  curl "${args[@]}" -H @<(printf 'Authorization: Bearer %s\n' "$token") "$url"
else
  curl "${args[@]}" "$url"
fi

# A .safetensors file starts with an 8-byte header length, then "{". Anything
# else is usually a login or error page served in place of the file.
if [ "$(head -c 9 "$tmp" | tail -c 1)" != "{" ]; then
  echo "The download isn't a .safetensors file (a login page?); check the link and key" >&2
  exit 1
fi
chmod 644 "$tmp"
mv "$tmp" "$dest/$file"
echo "saved loras/$subdir/$file ($(du -h "$dest/$file" | cut -f1))"
