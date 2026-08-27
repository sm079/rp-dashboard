"""LoRA library: LoRAs added by pasting a CivitAI or Hugging Face link, kept per model set.

The library (.state/loras.json) remembers each LoRA's name and download link. rp.py
downloads a set's LoRAs onto the pod, into models/loras/<set>/, whenever that set is on
the pod; the dashboard also downloads a newly added one straight away on a running pod.
"""
import hashlib
import json
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LIBRARY = Path(__file__).resolve().parent / ".state" / "loras.json"
UA = "runpod-helper/0.1"
CIVITAI_API = "https://civitai.com/api/v1"
LORA_TYPES = {"LORA", "LoCon", "DoRA", "LyCORIS"}
REDIRECTS = (301, 302, 303, 307, 308)
_lock = threading.Lock()


class LoraError(Exception):
    """A link that can't be added, with a message fit to show the user."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would resend the Authorization header to the redirect target (a CDN).
    def redirect_request(self, *args, **kwargs):
        return None


def _ssl_context():
    """Default trust; on Windows only the root store.

    Windows' intermediate store can hold expired copies of certificates that a server
    sends a valid chain for, and OpenSSL 1.1.1 (Python 3.11 and older on Windows) then
    fails with "certificate has expired" (seen with civitai.com).
    """
    if sys.platform != "win32":
        return ssl.create_default_context()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    roots = bytearray()
    for cert, encoding, trust in ssl.enum_certificates("ROOT"):
        if encoding == "x509_asn" and (trust is True or ssl.Purpose.SERVER_AUTH.oid in trust):
            roots.extend(cert)
    ctx.load_verify_locations(cadata=bytes(roots))
    return ctx


_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ssl_context()))


def load():
    try:
        return json.loads(LIBRARY.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _save(items):
    LIBRARY.parent.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(LIBRARY)


def add(set_key, url, civitai_key=None, hf_token=None):
    """Look up a pasted link and remember it under a model set. Returns the new entry."""
    item = resolve(url.strip(), civitai_key, hf_token)
    item = {"id": f"{set_key}/{item.pop('ref')}", "set": set_key, **item, "added": time.time()}
    with _lock:
        items = load()
        for other in items:
            if other["id"] == item["id"]:
                raise LoraError(f"{other['name']} is already in the list.")
            if other["set"] == set_key and other["file"] == item["file"]:
                raise LoraError(f"{other['name']} is already saved as {item['file']}.")
        items.append(item)
        _save(items)
    return item


def remove(lora_id):
    """Forget a LoRA. Returns the removed entry, or None if it wasn't in the list."""
    with _lock:
        items = load()
        gone = next((i for i in items if i["id"] == lora_id), None)
        if gone:
            _save([i for i in items if i["id"] != lora_id])
    return gone


def resolve(url, civitai_key=None, hf_token=None):
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise LoraError("Paste a full https:// link from CivitAI or Hugging Face.")
    if re.fullmatch(r"(www\.)?civitai\.[a-z]+", host):
        return _civitai(parts, civitai_key)
    if host in ("huggingface.co", "www.huggingface.co", "hf.co"):
        return _huggingface(parts, hf_token)
    raise LoraError("Only CivitAI and Hugging Face links are supported.")


def safe_file(name):
    """A plain .safetensors file name: no folders, no hidden files, no odd characters."""
    name = re.sub(r"[^A-Za-z0-9._()+\-]+", "_", name.replace("\\", "/").rsplit("/", 1)[-1])
    name = name.lstrip(".")
    if not name.lower().endswith(".safetensors") or len(name) <= len(".safetensors"):
        raise LoraError("Only .safetensors files are supported.")
    return name


# --- CivitAI ----------------------------------------------------------------

def _civitai(parts, key):
    """Model page (latest version, or ?modelVersionId=) or an /api/download/models/<id> link."""
    version = (urllib.parse.parse_qs(parts.query).get("modelVersionId") or [""])[0]
    model = re.match(r"/models/(\d+)", parts.path)
    download = re.match(r"/api/download/models/(\d+)", parts.path)
    if download:
        version = download.group(1)
    if version:
        if not version.isdigit():
            raise LoraError("That CivitAI link has an invalid modelVersionId.")
        info = _civitai_get(f"/model-versions/{version}", key)
    elif model:
        data = _civitai_get(f"/models/{model.group(1)}", key)
        if not data.get("modelVersions"):
            raise LoraError("That CivitAI model has no versions to download.")
        info = data["modelVersions"][0]  # newest first
        info.setdefault("modelId", data.get("id"))
        info.setdefault("model", {"name": data.get("name"), "type": data.get("type")})
    else:
        raise LoraError("That CivitAI link doesn't point to a model.")

    kind = (info.get("model") or {}).get("type")
    if kind not in LORA_TYPES:
        raise LoraError(f"That's a {kind or 'file'} on CivitAI, not a LoRA.")
    files = [f for f in info.get("files") or [] if f.get("type") == "Model"
             and (f.get("metadata") or {}).get("format") == "SafeTensor"]
    if not files:
        raise LoraError("That CivitAI version has no .safetensors file.")
    f = next((f for f in files if f.get("primary")), files[0])
    link = f.get("downloadUrl") or ""
    # The key is sent to this link, so it has to be CivitAI's own.
    if urllib.parse.urlsplit(link).hostname != "civitai.com":
        raise LoraError("CivitAI returned an unexpected download link.")
    return {
        "ref": f"civitai-{info['id']}",
        "source": "civitai",
        "name": (info.get("model") or {}).get("name") or f["name"],
        "version": info.get("name") or "",
        "base": info.get("baseModel") or "",
        "triggers": [w.strip() for w in info.get("trainedWords") or [] if w and w.strip()][:20],
        "file": safe_file(f["name"]),
        "size": int((f.get("sizeKB") or 0) * 1024),
        "download": link,
        "auth": "civitai",
        "page": urllib.parse.urlunsplit(parts),
    }


def _civitai_get(path, key):
    headers = {"User-Agent": UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with _opener.open(urllib.request.Request(CIVITAI_API + path, headers=headers), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LoraError("CivitAI has no such model or version.") from None
        if e.code in (401, 403):
            hint = "check CIVITAI_API_KEY" if key else "set CIVITAI_API_KEY"
            raise LoraError(f"CivitAI refused the request ({e.code}); {hint}.") from None
        raise LoraError(f"CivitAI answered {e.code}; try again later.") from None
    except (OSError, ValueError) as e:
        raise LoraError(f"Couldn't reach CivitAI: {e}") from None


# --- Hugging Face -----------------------------------------------------------

def _huggingface(parts, token):
    """A file link: https://huggingface.co/<owner>/<repo>/(resolve|blob)/<revision>/<path>."""
    m = re.fullmatch(r"/([^/]+)/([^/]+)/(?:resolve|blob)/([^/]+)/(.+)", parts.path)
    if not m:
        raise LoraError("Paste a direct Hugging Face file link (…/resolve/main/<name>.safetensors).")
    owner, repo, rev, path = m.groups()
    file = safe_file(urllib.parse.unquote(path))
    link = f"https://huggingface.co/{owner}/{repo}/resolve/{rev}/{path}"
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        # Large files answer with a redirect to the CDN, which carries the size.
        with _opener.open(urllib.request.Request(link, method="HEAD", headers=headers), timeout=20) as r:
            size = int(r.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as e:
        if e.code in REDIRECTS:
            size = int(e.headers.get("X-Linked-Size") or 0)
        elif e.code in (401, 403):
            hint = "your HF_TOKEN has no access to it" if token else "set HF_TOKEN"
            raise LoraError(f"Hugging Face restricts this file; {hint}.") from None
        elif e.code == 404:
            raise LoraError("Hugging Face has no such file.") from None
        else:
            raise LoraError(f"Hugging Face answered {e.code}; try again later.") from None
    except OSError as e:
        raise LoraError(f"Couldn't reach Hugging Face: {e}") from None
    return {
        "ref": "hf-" + hashlib.sha1(f"{owner}/{repo}/{rev}/{path}".encode()).hexdigest()[:12],
        "source": "hf",
        "name": urllib.parse.unquote(path).rsplit("/", 1)[-1][: -len(".safetensors")],
        "version": "" if rev == "main" else rev,
        "base": f"{owner}/{repo}",
        "triggers": [],
        "file": file,
        "size": size,
        "download": link,
        "auth": "hf",
        "page": f"https://huggingface.co/{owner}/{repo}/blob/{rev}/{path}",
    }
