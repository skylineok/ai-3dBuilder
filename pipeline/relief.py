"""透光浮雕：照片亮度 -> 板厚 -> 带边框的实体挂板。

为什么人像不做三维重建：单张照片看不到背面，重建只能靠先验猜，
人脸和手指恰好最依赖多视角约束。浮雕只表达朝向相机这一面，
2D 的五官轮廓和皮肤起伏都能保住，所以人像/照片一律走这条路。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

__all__ = ["make_lithophane"]


def _grid_mesh(height: np.ndarray, width_mm: float, plate_mm: float = 0.0) -> trimesh.Trimesh:
    """把高度场做成「顶面起伏 + 底面平整」的封闭实体。

    height 的 y 方向必须已经与图像行序一致（调用方负责翻转）。
    """
    rows, cols = height.shape
    pixel = width_mm / (cols - 1)
    height_mm = plate_mm + height

    xs = np.linspace(0.0, width_mm, cols)
    ys = np.linspace(0.0, pixel * (rows - 1), rows)
    gx, gy = np.meshgrid(xs, ys)
    top = np.column_stack([gx.ravel(), gy.ravel(), height_mm.ravel()])

    idx = np.arange(rows * cols).reshape(rows, cols)
    a = idx[:-1, :-1].ravel()
    b = idx[:-1, 1:].ravel()
    c = idx[1:, 1:].ravel()
    d = idx[1:, :-1].ravel()
    faces = np.vstack([np.column_stack([a, b, c]), np.column_stack([a, c, d])])

    # 底面：沿顶面边界复制一圈平移到 z=0，侧壁连接两圈，底面用扇形封口
    border = np.concatenate([idx[0, :], idx[1:, -1], idx[-1, -2::-1], idx[-2:0:-1, 0]])
    ring_top = top[border]
    ring_bottom = ring_top.copy()
    ring_bottom[:, 2] = 0.0

    base = len(top)
    n = len(border)
    vertices = np.vstack([top, ring_bottom, [[width_mm / 2, ys[-1] / 2, 0.0]]])
    center = base + n

    for k in range(n):
        t0, t1 = int(border[k]), int(border[(k + 1) % n])
        b0, b1 = base + k, base + (k + 1) % n
        faces = np.vstack([faces, [[t0, b0, b1], [t0, b1, t1], [center, b1, b0]]])

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    trimesh.repair.fix_normals(mesh, multibody=True)
    return mesh


def make_lithophane(
    image_path: str | Path,
    *,
    width_mm: float = 120.0,
    min_mm: float = 0.8,
    max_mm: float = 3.2,
    frame_mm: float = 8.0,
    frame_height_mm: float = 4.0,
    max_grid: int = 400,
    gamma: float = 0.9,
) -> tuple[trimesh.Trimesh, dict]:
    """照片亮度决定厚度：越暗越厚，背光时透射率差异就是照片本身。"""
    from PIL import Image
    from scipy import ndimage

    image = Image.open(image_path).convert("L")
    luma = np.asarray(image, dtype=np.float32) / 255.0
    luma = ndimage.gaussian_filter(luma, sigma=0.6)
    luma = luma[::-1]  # 网格 +Y 向上，图像行号向下，翻一次才不会倒立

    thickness = min_mm + (1.0 - luma) ** gamma * (max_mm - min_mm)

    rows, cols = thickness.shape
    step = max(1, int(np.ceil(max(rows, cols) / max_grid)))
    small = thickness[::step, ::step]

    band = max(2, int(round(frame_mm / (width_mm / (small.shape[1] - 1)))))
    small[:band, :] = frame_height_mm
    small[-band:, :] = frame_height_mm
    small[:, :band] = frame_height_mm
    small[:, -band:] = frame_height_mm

    mesh = _grid_mesh(small, width_mm)
    info = {
        "mode": "lithophane",
        "width_mm": width_mm,
        "height_mm": round(float(mesh.extents[1]), 2),
        "thickness_mm": round(float(mesh.extents[2]), 2),
        "range_mm": [min_mm, max_mm],
        "frame_mm": frame_mm,
        "faces": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
    }
    return mesh, info
