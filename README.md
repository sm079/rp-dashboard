# runpod-helper

Run ComfyUI on a RunPod GPU pod that is reachable **only over SSH**, with its
inputs, outputs and workflows synced to a local folder.

- The pod is launched from RunPod's official ComfyUI template with only port
  22 exposed: no public proxy URLs for ComfyUI, Jupyter or the file browser.
- ComfyUI is restarted to listen on `127.0.0.1` and to keep its input, output,
  temp and user (workflows/settings) folders in RAM (`/dev/shm`).
- `connect` opens an SSH tunnel to `http://localhost:8288` (so a local
  ComfyUI can keep 8188) and syncs the local `sync/` folder with the pod:

  | Folder            | Direction    | Notes                                        |
  |-------------------|--------------|----------------------------------------------|
  | `sync/outputs/`   | pod → local  | in `<YYYY-MM-DD>/` folders; never overwrites |
  | `sync/inputs/`    | both ways    | ComfyUI's input folder                       |
  | `sync/workflows/` | both ways    | saved workflows                              |
  | `sync/settings/`  | both ways    | ComfyUI settings and keybindings             |
  | `sync/loras/`     | local → pod  | an extra LoRA folder in the pod's RAM        |

  For both-way folders the latest change wins, and deletions are mirrored
  both ways; deleting a local LoRA removes it from the pod. Outputs are never
  deleted on either side. Safeguards: a pod restart (which wipes its RAM) is
  detected and deletes nothing locally, a pod-side deletion must show in two
  consecutive listings, a locally edited file is never deleted, and a sudden
  mass deletion on the pod is refused with a warning. On connect, local files
  are uploaded first, so a fresh pod gets your workflows and settings.
- Nothing persists on RunPod: models are re-downloaded on each `up`.

## Requirements

- Python 3.9+ (standard library only) and the OpenSSH client.
- `RUNPOD_API_KEY` set in the environment.
- An SSH public key added to the RunPod account (Settings → SSH Public Keys);
  the matching private key path goes in `config.json` (`ssh_key`).

## Usage

On Windows, double-click **`start.cmd`**: it launches a pod (or reuses a
running one), sets everything up, opens ComfyUI in the browser and keeps
syncing outputs. Press Ctrl+C in that window to stop syncing; it offers to
terminate the pod. **`stop.cmd`** copies anything unsaved and terminates the
pod. Closing the window without stopping leaves the pod running and billing.

The same from a shell:

```sh
python rp.py start       # up (if needed) + connect + open the browser
python rp.py up          # launch, set up, download default models (~71 GB, ~2 min)
python rp.py connect     # tunnel + sync; open http://localhost:8288
python rp.py down        # copy anything unsaved, then terminate the pod
```

Other commands: `status`, `ssh [command]`, `setup` (re-run the remote setup),
`models [set ...]` (download extra model sets).

## Configuration

- `config.json`: GPU type, cloud type, template, disk size, local port, sync
  folder, model profiles (`profiles`: name → model sets), the ComfyUI release to pin (`comfyui_version`) and extra
  custom nodes (`custom_nodes`, git URLs) installed on each setup.
- `models.json`: model sets (Hugging Face repo → files). The Comfy-Org repos
  mirror ComfyUI's `models/` layout, so files download straight into place.

| Set      | Contents                                                       | Size     |
|----------|----------------------------------------------------------------|----------|
| `krea2`  | Krea 2 Turbo + Raw (fp8), Qwen3-VL 4B text encoder, VAE        | ~30 GB   |
| `h3`     | MiniMax H3 reference-to-video (pruned int8), NVFP4 text encoder, VAEs, ref2v 4-step turbo LoRA, community turbo v4 EMA LoRA | ~42 GB |
