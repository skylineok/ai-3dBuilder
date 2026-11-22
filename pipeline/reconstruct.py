"""图 -> 网格：调用云端三维生成服务。

后处理链不关心网格从哪来，这里只约定一件事：
输入一张图，输出一个 trimesh.Trimesh（任意单位、脏、不水密都可以）。
"""

from __future__ import annotations

from pathlib import Path

import trimesh

__all__ = ["MESH_SUFFIXES", "load_input", "reconstruct", "available"]

MESH_SUFFIXES = {".obj", ".stl", ".ply", ".glb", ".gltf", ".3mf"}

ROOT = Path(__file__).resolve().parent.parent


def _replicate(
    image_path: Path,
    *,
    model: str = "tencent/hunyuan3d-2",
    inputs: dict | None = None,
    image_field: str = "image",
    **_: object,
) -> trimesh.Trimesh:
    from .cloud import download_mesh, load_mesh_file, run_replicate

    prepared = _cloud_image(image_path)
    urls = run_replicate(model, prepared, inputs=inputs, image_field=image_field)
    local = download_mesh(urls[0], ROOT / "outputs" / "cloud", stem="cloud")
    print(f"[云端] {model} 输出：{urls[0]}")
    return load_mesh_file(local)


def _fal(
    image_path: Path,
    *,
    model: str = "fal-ai/hunyuan3d/v2",
    inputs: dict | None = None,
    image_field: str = "image_url",
    **_: object,
) -> trimesh.Trimesh:
    from .cloud import download_mesh, load_mesh_file, run_fal

    prepared = _cloud_image(image_path)
    urls = run_fal(model, prepared, inputs=inputs, image_field=image_field)
    local = download_mesh(urls[0], ROOT / "outputs" / "cloud", stem="cloud")
    print(f"[云端] {model} 输出：{urls[0]}")
    return load_mesh_file(local)


def _tencent(
    image_path: Path,
    *,
    model: str = "hy-3d-3.1",
    inputs: dict | None = None,
    **_: object,
) -> trimesh.Trimesh:
    from .cloud import load_mesh_file
    from .tencent import generate, to_z_up

    local = generate(image_path, model=model, **(inputs or {}))
    return to_z_up(load_mesh_file(local))


def _cloud_image(image_path: Path) -> Path:
    """上传前抠图：透明底能明显提高云端重建的成功率和轮廓准确度。抠图失败就用原图。"""
    from PIL import Image

    image = Image.open(image_path).convert("RGBA")
    try:
        import rembg

        if image.getextrema()[3][0] == 255:
            image = rembg.remove(image)
    except Exception as exc:
        print(f"[预处理] 抠图跳过：{type(exc).__name__}: {exc}")

    out = ROOT / "outputs" / "cloud_input.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


_PROVIDERS = {
    "tencent": _tencent,
    "replicate": _replicate,
    "fal": _fal,
}


def available() -> list[str]:
    return sorted(_PROVIDERS)


def reconstruct(image_path: str | Path, provider: str = "tencent", **kwargs) -> trimesh.Trimesh:
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"找不到图片：{image_path}")
    if provider not in _PROVIDERS:
        raise ValueError(f"未知 provider：{provider}，可选 {available()}")
    return _PROVIDERS[provider](image_path, **kwargs)


def load_input(path: str | Path) -> trimesh.Trimesh:
    """直接读一个已有网格（调后处理参数时用）。"""
    path = Path(path)
    if path.suffix.lower() not in MESH_SUFFIXES:
        raise ValueError(f"不是网格文件：{path.name}，请用 --image 走重建")
    mesh = trimesh.load(path, process=False, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError(f"读不出三角网格：{path.name}")
    return mesh
