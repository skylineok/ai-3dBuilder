"""腾讯云 MaaS 网关的 3D 生成（hy-3d-3.1 / hy-3d-3.0 / hy-3d-express）。

实测可用的调用方式（2026-11）：
  POST /v1/api/3d/submit  {"model": "...", "image_base64": "..."}  -> id
  POST /v1/api/3d/query   {"model": "...", "id": "..."}            -> status + data

注意：图片必须走 image_base64。传 file.url / image.url 会被接口忽略，
报 "Prompt、ImageBase64、ImageUrl 不能同时为空"。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from .cloud import CloudError, load_key
from .reconstruct import ROOT

__all__ = ["generate", "submit", "wait"]

BASE = "https://tokenhub.tencentmaas.com"
DEFAULT_MODEL = "hy-3d-3.1"
DONE_STATES = {"completed", "succeeded", "success"}
FAIL_STATES = {"failed", "canceled", "cancelled"}


def _post(path: str, payload: dict, timeout: int = 120) -> dict:
    key = load_key("TENCENT_API_KEY")
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise CloudError(f"HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudError(f"网络错误：{exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise CloudError(f"返回不是 JSON：{body[:300]}") from exc


def _encode(image_path: Path, max_side: int = 1024) -> str:
    """压缩到合理边长再 base64。原图动辄 1279×2824，转 base64 会到 8MB 以上。"""
    image = Image.open(image_path).convert("RGBA")
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.LANCZOS)
    buffer = ROOT / "outputs" / "cloud_input.png"
    buffer.parent.mkdir(parents=True, exist_ok=True)
    image.save(buffer)
    return base64.b64encode(buffer.read_bytes()).decode("ascii")


def submit(image_path: Path, model: str = DEFAULT_MODEL, **params) -> str:
    payload = {"model": model, "image_base64": _encode(Path(image_path)), **params}
    result = _post("/v1/api/3d/submit", payload)
    if result.get("status") in FAIL_STATES:
        error = result.get("error") or {}
        raise CloudError(f"提交失败：{error.get('code')} {error.get('message')}")
    job_id = result.get("id")
    if not job_id:
        raise CloudError(f"提交后没有拿到任务号：{json.dumps(result)[:300]}")
    return str(job_id)


def wait(model: str, job_id: str, timeout: int = 900, poll: float = 8.0) -> list[dict]:
    deadline = time.time() + timeout
    while True:
        result = _post("/v1/api/3d/query", {"model": model, "id": job_id}, timeout=60)
        status = str(result.get("status", ""))
        if status in DONE_STATES:
            data = result.get("data") or []
            if not data:
                raise CloudError("任务完成但没有输出")
            return data
        if status in FAIL_STATES:
            error = result.get("error") or {}
            raise CloudError(f"生成失败：{error.get('code')} {error.get('message')}")
        if time.time() > deadline:
            raise CloudError(f"等待超时（{timeout}s），最后状态 {status}")
        time.sleep(poll)


def generate(
    image_path: Path | str,
    model: str = DEFAULT_MODEL,
    out_dir: Path | None = None,
    timeout: int = 900,
    **params,
) -> Path:
    """跑完整流程，返回下载到本地的网格路径（优先 GLB，带回顶点色）。"""
    image_path = Path(image_path)
    job_id = submit(image_path, model=model, **params)
    print(f"[腾讯云] 任务 {job_id} 已提交，等待结果…")
    data = wait(model, job_id, timeout=timeout)

    chosen = next((d for d in data if str(d.get("type")).lower() == "glb"), None)
    chosen = chosen or next((d for d in data if str(d.get("type")).lower() == "obj"), None)
    chosen = chosen or data[0]

    url = chosen.get("url")
    if not url:
        raise CloudError(f"输出里没有下载地址：{json.dumps(chosen)[:300]}")

    out_dir = out_dir or (ROOT / "outputs" / "cloud")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "." + str(chosen.get("type", "glb")).lower()
    target = out_dir / f"tencent{suffix}"

    request = urllib.request.Request(url, headers={"User-Agent": "ai-3d-builder/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        target.write_bytes(response.read())
    print(f"[腾讯云] 已下载 {target.name}（{target.stat().st_size // 1024} KB）")
    return target


def to_z_up(mesh):
    """glTF 约定 Y 轴朝上，本流水线约定 Z 轴朝上，差一个绕 X 轴 +90°。"""
    import trimesh

    mesh = mesh.copy()
    mesh.apply_transform(trimesh.transformations.rotation_matrix(3.141592653589793 / 2, [1, 0, 0]))
    return mesh
