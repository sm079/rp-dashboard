# runpod-helper

Run ComfyUI on a RunPod GPU pod that is reachable **only over SSH**, with its
inputs, outputs and workflows synced to a local folder.

- The pod is launched from RunPod's official ComfyUI template with only port
  22 exposed: no public proxy URLs for ComfyUI, Jupyter or the file browser.
- ComfyUI is restarted to listen on `127.0.0.1` and to keep its input, output,
  temp and user (workflows/settings) folders in RAM (`/dev/shm`).
- `connect` opens an SSH tunnel to `http://localhost:8188` and copies each
  finished output to `outputs/<YYYY-MM-DD>/`, each saved workflow to
  `workflows/`, and ComfyUI's settings to `settings/`. Workflows and settings
  are restored on the next `up`.
- Nothing persists on RunPod: models are re-downloaded on each `up`.

## Requirements

- Python 3.9+ (standard library only) and the OpenSSH client.
- `RUNPOD_API_KEY` set in the environment.
- An SSH public key added to the RunPod account (Settings → SSH Public Keys);
  the matching private key path goes in `config.json` (`ssh_key`).

## Usage

```sh
python rp.py up          # launch, set up, download default models (~71 GB, ~2 min)
python rp.py connect     # tunnel + sync; open http://localhost:8188
python rp.py down        # copy anything unsaved, then terminate the pod
```

Other commands: `status`, `ssh [command]`, `setup` (re-run the remote setup),
`models [set ...]` (download extra model sets).

## Configuration

- `config.json`: GPU type, cloud type, template, disk size, local folders,
  default model sets, the ComfyUI release to pin (`comfyui_version`) and extra
  custom nodes (`custom_nodes`, git URLs) installed on each setup.
- `models.json`: model sets (Hugging Face repo → files). The Comfy-Org repos
  mirror ComfyUI's `models/` layout, so files download straight into place.

| Set      | Contents                                                       | Size     |
|----------|----------------------------------------------------------------|----------|
| `krea2`  | Krea 2 Turbo + Raw (fp8), Qwen3-VL 4B text encoder, VAE        | ~30 GB   |
| `h3`     | MiniMax H3 reference-to-video (pruned int8), NVFP4 text encoder, VAEs, ref2v 4-step turbo LoRA, community turbo v4 EMA LoRA | ~42 GB |
