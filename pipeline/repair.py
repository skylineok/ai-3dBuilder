"""把单图重建出来的脏网格修成流形实体。

修复顺序是刻意的：先合并顶点（拓扑错误大多源于顶点没焊上），
再去碎片，再补洞，最后校准法线。顺序反过来会让补洞补出错面。
"""

from __future__ import annotations

import numpy as np
import trimesh
from scipy import ndimage
from skimage import measure

__all__ = [
    "weld",
    "drop_small_parts",
    "make_solid",
    "voxel_solidify",
    "reduce_faces",
    "mesh_status",
]


def weld(mesh: trimesh.Trimesh, tol: float = 1e-5) -> trimesh.Trimesh:
    """按容差焊接重合顶点，并移除退化面与重复面。"""
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        raise ValueError("网格为空")

    grid = np.round(np.asarray(mesh.vertices, dtype=np.float64) / tol).astype(np.int64)
    uniq, inverse = np.unique(grid, axis=0, return_inverse=True)
    faces = inverse.reshape(-1)[mesh.faces]

    keep = (
        (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 0] != faces[:, 2])
    )
    out = trimesh.Trimesh(vertices=uniq * tol, faces=faces[keep], process=False)
    out.remove_unreferenced_vertices()
    return out


def drop_small_parts(mesh: trimesh.Trimesh, min_area_ratio: float = 0.01) -> trimesh.Trimesh:
    """丢掉面积占比过小的连通块（重建模型常见的漂浮碎屑）。"""
    parts = mesh.split(only_watertight=False)
    if len(parts) <= 1:
        return mesh

    areas = np.array([float(p.area) for p in parts])
    keep = areas >= areas.max() * min_area_ratio
    if not keep.any():
        keep[int(areas.argmax())] = True
    kept = [p for p, k in zip(parts, keep) if k]
    return trimesh.util.concatenate(kept)


def voxel_solidify(
    mesh: trimesh.Trimesh,
    resolution: int = 180,
    pitch: float | None = None,
    max_closing: int = 32,
    min_fill_gain: float = 1.05,
) -> tuple[trimesh.Trimesh, dict]:
    """体素兜底：逐级闭合裂缝 -> 填充内部 -> 重新提取外壳。

    闭合半径要自适应：裂缝小时 1-2 个体素就够，大洞需要几十个。
    所以从 1 试到 max_closing，直到"填充确实吃进了内部"为止。
    这是保证水密的最后手段，代价是细节会变钝。
    """
    extents = np.asarray(mesh.extents, dtype=np.float64)
    if pitch is None:
        pitch = float(max(extents.max() / max(resolution, 8), 1e-9))

    grid = mesh.voxelized(pitch=pitch)
    surface = np.asarray(grid.matrix, dtype=bool)
    if not surface.any():
        raise ValueError("体素化结果为空，网格可能退化")

    structure = ndimage.generate_binary_structure(3, 1)
    limit = max(2, min(surface.shape) // 4)

    solid = None
    used = 0
    sealed = False
    # 必须是"膨胀 -> 填充 -> 腐蚀"，不能直接用 binary_closing：
    # 闭合是先膨胀再腐蚀，中间没有填内部的机会，大洞依然漏。
    for iterations in (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48):
        steps = min(iterations, limit)
        dilated = ndimage.binary_dilation(surface, structure=structure, iterations=steps)
        filled = ndimage.binary_fill_holes(dilated)
        used = steps
        if filled.sum() > dilated.sum() * 1.02:
            eroded = ndimage.binary_erosion(filled, structure=structure, iterations=steps)
            if eroded.any():
                solid = ndimage.binary_fill_holes(eroded)
                sealed = True
                break
        if steps >= limit:
            # 已经到极限半径仍封不住，保留膨胀后填实的体，至少实心可打印
            solid = filled
            break

    if solid is None:
        solid = ndimage.binary_fill_holes(surface)

    vertices, faces, _, _ = measure.marching_cubes(
        np.pad(solid, 1).astype(np.float32), level=0.5
    )
    vertices = trimesh.transform_points(vertices - 1.0, grid.transform)

    out = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    trimesh.repair.fix_normals(out, multibody=True)

    meta = {
        "pitch": float(pitch),
        "closing_iterations": int(used),
        "solid_voxels": int(solid.sum()),
        "sealed": bool(sealed),
    }
    return out, meta


def _decimate_pymeshlab(mesh: trimesh.Trimesh, max_faces: int) -> trimesh.Trimesh:
    """拓扑约束的二次误差简化。

    空心壳有内外两层曲面，且相距只有一两毫米，普通简化会把两层缝到一起
    造成非流形。pymeshlab 这里是唯一能稳定保住水密性的做法。
    """
    import pymeshlab

    meshset = pymeshlab.MeshSet()
    meshset.add_mesh(
        pymeshlab.Mesh(
            vertex_matrix=np.asarray(mesh.vertices, dtype=np.float64),
            face_matrix=np.asarray(mesh.faces, dtype=np.int32),
        )
    )
    meshset.meshing_decimation_quadric_edge_collapse(
        targetfacenum=int(max_faces),
        preservetopology=True,
        preserveboundary=True,
        optimalplacement=True,
        planarquadric=True,
        qualitythr=0.5,
    )
    current = meshset.current_mesh()
    return trimesh.Trimesh(
        vertices=current.vertex_matrix(), faces=current.face_matrix(), process=True
    )


def reduce_faces(mesh: trimesh.Trimesh, max_faces: int | None) -> trimesh.Trimesh:
    """简化面数，控制 STL 体积与切片时间。

    先用快的 fast_simplification；一旦它破坏水密，再退回带拓扑约束的
    pymeshlab。都失败就保留原网格——文件大是麻烦，不水密是打不出来。
    """
    if not max_faces or len(mesh.faces) <= max_faces:
        return mesh

    try:
        out = mesh.simplify_quadric_decimation(face_count=int(max_faces))
        if not mesh.is_watertight or out.is_watertight:
            return out
    except Exception:
        pass

    try:
        alt = _decimate_pymeshlab(mesh, max_faces)
        if alt.is_watertight and alt.volume > 0:
            return alt
    except Exception:
        pass

    return mesh


def mesh_status(mesh: trimesh.Trimesh) -> dict:
    return {
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "volume": float(round(mesh.volume, 6)),
        "euler_number": int(mesh.euler_number),
    }


def make_solid(
    mesh: trimesh.Trimesh,
    *,
    min_part_ratio: float = 0.01,
    voxel_resolution: int = 180,
    max_faces: int | None = None,
    smooth_iterations: int = 0,
) -> tuple[trimesh.Trimesh, dict]:
    """完整修复链，返回实体网格与处理记录。"""
    notes: list[str] = []
    raw_faces = len(mesh.faces)

    cleaned = weld(mesh)
    if len(cleaned.faces) != raw_faces:
        notes.append(f"焊接/去退化面：{raw_faces} -> {len(cleaned.faces)}")

    cleaned = drop_small_parts(cleaned, min_part_ratio)
    trimesh.repair.fill_holes(cleaned)
    try:
        trimesh.repair.fix_normals(cleaned, multibody=True)
        trimesh.repair.fix_inversion(cleaned)
    except Exception as exc:  # 法线修复在非流形网格上可能抛错，不阻塞后续兜底
        notes.append(f"法线修复跳过：{type(exc).__name__}")

    used_fallback = False
    if not (cleaned.is_watertight and cleaned.volume > 0):
        notes.append("补洞后仍不水密，切体素重建")
        resolution = int(voxel_resolution)
        cleaned, voxel_meta = voxel_solidify(cleaned, resolution=resolution)
        # marching cubes 出多少面由体素分辨率决定，超预算就降分辨率重来，
        # 而不是事后简化——简化会把水密搞坏。
        retries = 0
        while max_faces and len(cleaned.faces) > max_faces * 1.4 and retries < 2:
            resolution = max(int(resolution * (max_faces / len(cleaned.faces)) ** 0.5), 40)
            cleaned, voxel_meta = voxel_solidify(cleaned, resolution=resolution)
            retries += 1
        notes.append(
            f"体素重建：分辨率 {resolution}，闭合半径 {voxel_meta['closing_iterations']} 体素"
            + ("" if voxel_meta["sealed"] else "（未完全封住，外壳保底）")
        )
        used_fallback = True

    if smooth_iterations > 0:
        try:
            trimesh.smoothing.filter_laplacian(cleaned, iterations=int(smooth_iterations))
            notes.append(f"拉普拉斯平滑 {smooth_iterations} 次")
        except Exception as exc:
            notes.append(f"平滑跳过：{type(exc).__name__}")

    cleaned = reduce_faces(cleaned, max_faces)

    info = {
        "fallback_voxel": used_fallback,
        "notes": notes,
        **mesh_status(cleaned),
    }
    return cleaned, info
