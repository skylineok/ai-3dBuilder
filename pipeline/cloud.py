"""云端重建：通过 Replicate / fal 跑更强的模型（Hunyuan3D、TRELLIS 等）。

本地只有 TripoSR 这一档前馈模型，单图重建的天花板就在那里；要更好的形态
只能换模型，而这类模型需要 GPU。这两个服务都是「提交任务 -> 轮询 -> 拿
结果 URL -> 下载网格」，所以共用同一套下载与解析逻辑。

密钥从环境变量或仓库根目录的 .env 读取，不要写进代码。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import trimesh

__all__ = ["CloudError", "load_key", "run_replicate", "run_fal", "download_mesh"]

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
MESH_SUFFIXES = (".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".zip")


class CloudError(RuntimeError):
    """云端调用失败，消息里带上 HTTP 状态和接口原文，方便定位。"""


def load_key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() == name:
                value = raw.strip().strip('"').strip("'")
                if value:
                    return value
    raise CloudError(
        f"缺少密钥 {name}。把它写进 {ENV_FILE}，例如：\n  {name}=xxxxx"
    )


def _request(url: str, headers: dict, payload: dict | None = None, timeout: int = 120) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}

    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise CloudError(f"HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudError(f"网络错误：{exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise CloudError(f"接口返回的不是 JSON：{body[:300]}") from exc


def _image_payload(image_path: Path) -> str:
    """转成 data URI，省得为了一个文件去开图床。"""
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _collect_mesh_urls(node, found: list[str]) -> list[str]:
    if isinstance(node, str):
        lower = node.lower()
        if lower.endswith(MESH_SUFFIXES) or "/output/" in lower:
            found.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_mesh_urls(value, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect_mesh_urls(value, found)
    return found


def run_replicate(
    model: str,
    image_path: Path,
    *,
    inputs: dict | None = None,
    image_field: str = "image",
    token: str | None = None,
    timeout: int = 900,
    poll: float = 5.0,
) -> list[str]:
    """跑一个 Replicate 模型，返回结果里的网格 URL 列表。"""
    token = token or load_key("REPLICATE_API_TOKEN")
    headers = {"Authorization": f"Token {token}"}

    payload = {**(inputs or {}), image_field: _image_payload(image_path)}
    prediction = _request(
        f"https://api.replicate.com/v1/models/{model}/predictions", headers, {"input": payload}
    )

    deadline = time.time() + timeout
    while prediction.get("status") not in {"succeeded", "failed", "canceled"}:
        if time.time() > deadline:
            raise CloudError(f"等待超时（{timeout}s），任务仍在 {prediction.get('status')}")
        time.sleep(poll)
        poll_url = (prediction.get("urls") or {}).get("get")
        if not poll_url:
            raise CloudError(f"结果里没有轮询地址：{prediction}")
        prediction = _request(poll_url, headers)

    if prediction.get("status") != "succeeded":
        raise CloudError(f"任务失败：{prediction.get('error') or prediction.get('status')}")

    urls = _collect_mesh_urls(prediction.get("output"), [])
    if not urls:
        raise CloudError(f"没在输出里找到网格文件：{json.dumps(prediction.get('output'))[:400]}")
    return urls


def run_fal(
    endpoint: str,
    image_path: Path,
    *,
    inputs: dict | None = None,
    image_field: str = "image_url",
    key: str | None = None,
    timeout: int = 900,
) -> list[str]:
    """跑一个 fal 模型（同步接口），返回网格 URL。"""
    key = key or load_key("FAL_KEY")
    payload = {**(inputs or {}), image_field: _image_payload(image_path)}
    result = _request(
        f"https://fal.run/{endpoint}",
        {"Authorization": f"Key {key}"},
        payload,
        timeout=timeout,
    )
    urls = _collect_mesh_urls(result, [])
    if not urls:
        raise CloudError(f"没在输出里找到网格文件：{json.dumps(result)[:400]}")
    return urls


def download_mesh(url: str, out_dir: Path, stem: str = "cloud") -> Path:
    """下载并解压（如果给的是 zip），返回本地网格路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(url.split("?")[0]).suffix.lower() or ".glb"
    target = out_dir / f"{stem}{suffix}"

    request = urllib.request.Request(url, headers={"User-Agent": "ai-3d-builder/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        target.write_bytes(response.read())

    if target.suffix.lower() == ".zip":
        import zipfile

        with zipfile.ZipFile(target) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(MESH_SUFFIXES)]
            if not names:
                raise CloudError(f"压缩包里没有网格：{archive.namelist()[:10]}")
            preferred = next((n for n in names if n.lower().endswith((".glb", ".obj"))), names[0])
            archive.extract(preferred, out_dir)
            return out_dir / preferred

    return target


def load_mesh_file(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, process=False, force="mesh")
    if isinstance(loaded, trimesh.Scene):
        loaded = trimesh.util.concatenate(tuple(loaded.geometry.values()))
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise CloudError(f"下载的文件里没有三角网格：{path.name}")
    return loaded
