"""打印规范化：姿态、切平底、真实尺寸、底板、可打印性体检。

这里所有长度单位都是毫米。trimesh 本身没有单位概念，
所以整条链路默认输入是任意单位，进到这里第一步就统一缩放到目标毫米高度。
"""

from __future__ import annotations

import numpy as np
import trimesh
from scipy import ndimage
from shapely.geometry import MultiPoint, Polygon
from skimage import measure

from .repair import mesh_status, reduce_faces, voxel_solidify

__all__ = [
    "make_printable",
    "inspect_print",
    "cut_below",
    "cut_above",
    "hollow_out",
    "auto_floor_z",
]

_UP = np.array([0.0, 0.0, -1.0])


def _rotation_to_down(direction: np.ndarray) -> np.ndarray:
    """构造把 direction 转到 -Z 的旋转矩阵。"""
    a = np.asarray(direction, dtype=float)
    a /= np.linalg.norm(a)
    b = _UP
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    s = float(np.linalg.norm(v))

    if s < 1e-12:
        if c > 0:
            return np.eye(4)
        axis = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        axis = np.cross(a, axis)
        axis /= np.linalg.norm(axis)
        return trimesh.transformations.rotation_matrix(np.pi, axis)

    k = v / s
    skew = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    rot = np.eye(3) + s * skew + (1.0 - c) * (skew @ skew)
    matrix = np.eye(4)
    matrix[:3, :3] = rot
    return matrix


def _hull_area(mesh: trimesh.Trimesh, max_points: int = 4000) -> float:
    pts = np.asarray(mesh.vertices[:, :2], dtype=float)
    if len(pts) > max_points:
        pts = pts[:: max(len(pts) // max_points, 1)]
    try:
        return float(MultiPoint(pts).convex_hull.area)
    except Exception:
        return 0.0


def _down_flat_ratio(mesh: trimesh.Trimesh, angle_deg: float = 20.0) -> float:
    normals = mesh.face_normals
    areas = mesh.area_faces
    limit = -np.cos(np.radians(angle_deg))
    return float(areas[normals[:, 2] < limit].sum() / max(mesh.area, 1e-12))


def choose_orientation(mesh: trimesh.Trimesh) -> tuple[np.ndarray, list[dict]]:
    """在六个轴向里挑一个"朝下最稳"的姿态。

    评分 = 平整朝下面占比 + 沿该轴的高度占比。不能用投影足迹面积：
    细高物件（花瓶、人偶）一侧躺，足迹反而更大，会被误判成最优。
    加入高度项以后，高物件保持直立，而盘子这类扁平件仍因为朝下面积大
    而选择平躺。
    """
    candidates = [
        (0, 0, -1), (0, 0, 1),
        (1, 0, 0), (-1, 0, 0),
        (0, 1, 0), (0, -1, 0),
    ]
    scored = []
    for direction in candidates:
        transform = _rotation_to_down(np.array(direction, dtype=float))
        rotated = mesh.copy()
        rotated.apply_transform(transform)
        scored.append(
            {
                "direction": direction,
                "flat": _down_flat_ratio(rotated),
                "height": float(rotated.extents[2]),
                "transform": transform,
            }
        )

    max_height = max(item["height"] for item in scored) or 1.0
    for item in scored:
        item["score"] = 0.6 * item["flat"] + 0.4 * (item["height"] / max_height)

    best = max(scored, key=lambda item: item["score"])
    table = [
        {
            "direction": list(item["direction"]),
            "flat": round(item["flat"], 3),
            "height": round(item["height"], 3),
            "score": round(item["score"], 3),
        }
        for item in scored
    ]
    return best["transform"], table


def auto_floor_z(
    mesh: trimesh.Trimesh,
    target_ratio: float = 0.16,
    max_fraction: float = 0.15,
    resolution: int = 110,
) -> float:
    """找"切到哪儿能获得足够接触面积"的高度。

    做法是按高度统计填充体素数，取从底部往上第一次达到
    最大截面积 target_ratio 的层。这比固定切 2% 更稳：
    圆底物件需要多切一点，平底物件几乎不用切。
    """
    extents = np.asarray(mesh.extents, dtype=float)
    if extents.max() <= 0:
        return float(mesh.bounds[0][2])

    pitch = float(extents.max() / max(resolution, 8))
    grid = mesh.voxelized(pitch=pitch)
    matrix = np.asarray(grid.matrix, dtype=bool)
    filled = ndimage.binary_fill_holes(matrix)
    if not filled.any():
        return float(mesh.bounds[0][2])

    profile = filled.sum(axis=(0, 1)).astype(float)
    transform = grid.transform
    zs = transform[2, 2] * np.arange(len(profile)) + transform[2, 3]

    limit = mesh.bounds[0][2] + extents[2] * max_fraction
    target = profile.max() * target_ratio
    for k in range(len(profile)):
        if zs[k] > limit:
            break
        if profile[k] >= target:
            return float(zs[k])
    return float(zs[0])


def cut_below(mesh: trimesh.Trimesh, z: float) -> trimesh.Trimesh | None:
    """用布尔差把 z 以下全部切掉，得到平整贴床面。"""
    low, high = mesh.bounds
    size = np.maximum(high - low, 1e-6)
    height = float(size[2] * 4 + 20)
    box = trimesh.creation.box(extents=[size[0] * 4 + 20, size[1] * 4 + 20, height])
    box.apply_translation([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, z - height / 2])

    try:
        cut = trimesh.boolean.difference([mesh, box], engine="manifold")
    except Exception:
        return None
    if cut is None or len(cut.faces) == 0:
        return None
    if not (cut.is_watertight and cut.volume > 0):
        return None
    return cut


def cut_above(mesh: trimesh.Trimesh, z: float) -> trimesh.Trimesh | None:
    """把 z 以上切掉，用于给容器开口。"""
    low, high = mesh.bounds
    size = np.maximum(high - low, 1e-6)
    height = float(size[2] * 4 + 20)
    box = trimesh.creation.box(extents=[size[0] * 4 + 20, size[1] * 4 + 20, height])
    box.apply_translation([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, z + height / 2])

    try:
        cut = trimesh.boolean.difference([mesh, box], engine="manifold")
    except Exception:
        return None
    if cut is None or len(cut.faces) == 0 or not cut.is_watertight or cut.volume <= 0:
        return None
    return cut


def hollow_out(
    mesh: trimesh.Trimesh,
    wall_mm: float = 1.4,
    open_top_mm: float = 0.0,
    wall_top_mm: float | None = None,
    max_extent_voxels: int = 180,
    max_cells: int = 12_000_000,
) -> tuple[trimesh.Trimesh, dict]:
    """体素法抽壳：实心 -> 空腔，壁厚可沿高度变化。

    不用偏移曲面再做布尔，是因为单图重建的网格并不干净，偏移很容易自交；
    体素腐蚀对拓扑没有任何要求，代价是壁厚会被量化到体素栅格上。

    wall_top_mm 给定时壁厚从底部的 wall_mm 线性过渡到顶部，做成
    "下厚上薄"：底部受力大、顶部只是为了成型，能省不少料。
    """
    if wall_mm <= 0:
        return mesh, {}

    wall_top = float(wall_mm if wall_top_mm is None else wall_top_mm)
    extents = np.asarray(mesh.extents, dtype=float)
    if extents.max() <= 0:
        raise ValueError("网格尺寸为 0")

    # 保证最薄处至少跨 3 层体素，否则壳会被打穿
    pitch = max(min(wall_mm, wall_top) / 3.0, extents.max() / max_extent_voxels)

    for _ in range(4):
        grid = mesh.voxelized(pitch=pitch)
        surface = np.asarray(grid.matrix, dtype=bool)
        if surface.any():
            solid = ndimage.binary_fill_holes(surface)
            if solid.sum() <= surface.sum():
                solid = surface
            if solid.size <= max_cells:
                break
        pitch *= 1.3
    else:
        raise ValueError("网格过大，无法抽壳")

    # 到表面的距离场；取"距离 <= 壁厚"的部分就是壳
    distance = ndimage.distance_transform_edt(solid)
    nz = solid.shape[2]
    z_world = grid.transform[2, 2] * np.arange(nz) + grid.transform[2, 3]
    span = max(float(z_world[-1] - z_world[0]), 1e-9)
    ratio = np.clip((z_world - z_world[0]) / span, 0.0, 1.0)
    wall_profile = wall_mm + (wall_top - wall_mm) * ratio
    shell = solid & (distance <= (wall_profile / pitch)[None, None, :])

    if shell.sum() == solid.sum():
        raise ValueError(f"壁厚 {wall_mm} mm 超过实体半径，没有形成空腔")

    vertices, faces, _, _ = measure.marching_cubes(
        np.pad(shell, 1).astype(np.float32), level=0.5
    )
    vertices = trimesh.transform_points(vertices - 1.0, grid.transform)

    out = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    if out.volume < 0:
        out.invert()

    meta = {
        "pitch_mm": round(float(pitch), 3),
        "wall_mm": round(float(wall_mm), 2),
        "wall_top_mm": round(float(wall_top), 2),
        "faces": int(len(out.faces)),
        "open_top_mm": 0.0,
    }

    if open_top_mm > 0:
        z_top = float(out.bounds[1][2])
        cut = cut_above(out, z_top - float(open_top_mm))
        if cut is None:
            meta["open_top_failed"] = True
        else:
            out = cut
            meta["open_top_mm"] = float(open_top_mm)

    if not out.is_watertight:
        raise ValueError("抽壳结果不水密")
    return out, meta


def punch_drain(
    mesh: trimesh.Trimesh, diameter_mm: float = 2.5, count: int = 1
) -> tuple[trimesh.Trimesh, dict]:
    """在底面打竖直通孔。树脂打印需要它排出未固化树脂，FDM 可不开。"""
    if diameter_mm <= 0 or count <= 0:
        return mesh, {}

    z0 = float(mesh.bounds[0][2])
    band = mesh.vertices[mesh.vertices[:, 2] <= z0 + 0.4]
    if len(band) < 3:
        return mesh, {"error": "底面顶点不足"}

    cx, cy = float(band[:, 0].mean()), float(band[:, 1].mean())
    span_xy = band[:, :2].max(axis=0) - band[:, :2].min(axis=0)
    ring = 0.35 * float(min(span_xy)) / 2.0 if count > 1 else 0.0

    # 只穿透底壁，不要一路打到顶：高度取整体高度的三成，最多 10mm
    height = float(min(10.0, max(mesh.extents[2] * 0.3, 3.0)))
    cutters = []
    for i in range(int(count)):
        angle = 2.0 * np.pi * i / count
        cutter = trimesh.creation.cylinder(
            radius=float(diameter_mm) / 2.0, height=height, sections=32
        )
        cutter.apply_translation(
            [cx + ring * np.cos(angle), cy + ring * np.sin(angle), z0 + height / 2.0 - 1.0]
        )
        cutters.append(cutter)

    try:
        out = trimesh.boolean.difference([mesh] + cutters, engine="manifold")
    except Exception as exc:
        return mesh, {"error": f"布尔失败：{type(exc).__name__}"}

    if out is None or not out.is_watertight or out.volume <= 0:
        return mesh, {"error": "打孔后不水密"}
    return out, {
        "holes": int(count),
        "diameter_mm": float(diameter_mm),
        "ring_mm": round(ring, 2),
    }


def _tapered_column(
    center_xy: tuple[float, float],
    radius_bottom: float,
    radius_top: float,
    z0: float,
    z1: float,
    sections: int = 12,
) -> trimesh.Trimesh:
    """下粗上细的立柱：底部宽增加附着力，顶部细便于掰断。"""
    angles = np.linspace(0.0, 2.0 * np.pi, sections, endpoint=False)
    cx, cy = center_xy
    bottom = np.column_stack(
        [cx + radius_bottom * np.cos(angles), cy + radius_bottom * np.sin(angles), np.full(sections, z0)]
    )
    top = np.column_stack(
        [cx + radius_top * np.cos(angles), cy + radius_top * np.sin(angles), np.full(sections, z1)]
    )
    vertices = np.vstack([bottom, top, [[cx, cy, z0]], [[cx, cy, z1]]])
    bot_c, top_c = 2 * sections, 2 * sections + 1

    faces = []
    for i in range(sections):
        j = (i + 1) % sections
        faces.append([bot_c, j, i])
        faces.append([top_c, sections + i, sections + j])
        faces.append([i, j, sections + j])
        faces.append([i, sections + j, sections + i])
    return trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=False)


def make_supports(
    mesh: trimesh.Trimesh,
    *,
    angle_deg: float = 45.0,
    gap_mm: float = 0.2,
    spacing_mm: float = 2.0,
    tip_mm: float = 0.35,
    base_mm: float = 0.9,
    ground_z: float = 0.0,
) -> tuple[trimesh.Trimesh | None, dict]:
    """给悬垂面生成竖直支撑柱。

    只处理"下方是空的、能一路落到热床"的面：下方还有零件的面本来就自支撑。
    柱顶与模型留 gap_mm 间隙，切片时不会粘死，打完可以直接掰掉。
    ground_z 是热床平面：有底板时要传底板高度，否则射线会被底板挡住，
    误判成"下方有支撑"。
    """
    normals = np.asarray(mesh.face_normals)
    areas = np.asarray(mesh.area_faces)
    centers = np.asarray(mesh.triangles_center)

    threshold = -np.cos(np.radians(angle_deg))
    mask = (normals[:, 2] < threshold) & (centers[:, 2] > ground_z + 0.4)
    candidates = np.flatnonzero(mask)
    if len(candidates) == 0:
        return None, {"count": 0, "reason": "没有悬垂面"}

    rng = np.random.default_rng(0)
    weights = areas[candidates] / areas[candidates].sum()
    samples = min(6000, max(150, int(areas[candidates].sum() / (spacing_mm**2))))
    picks = rng.choice(candidates, size=samples, p=weights)

    points = centers[picks]
    # 用 2mm 网格去重，避免同一位置堆很多根
    keys = np.round(points[:, :2] / spacing_mm).astype(np.int64)
    _, unique_idx = np.unique(keys, axis=0, return_index=True)
    points = points[unique_idx]

    intersector = trimesh.ray.ray_triangle.RayMeshIntersector(mesh)
    origins = points - np.array([0.0, 0.0, 0.05])
    directions = np.tile(np.array([0.0, 0.0, -1.0]), (len(origins), 1))
    locations, ray_ids, _ = intersector.intersects_location(
        origins, directions, multiple_hits=False
    )

    blocked = np.zeros(len(origins), dtype=bool)
    for hit, ray_id in zip(locations, ray_ids):
        # 只有打到零件本体才算被撑住；打到热床/底板说明下方是空的
        if float(hit[2]) > ground_z + 0.1:
            blocked[ray_id] = True

    columns = []
    for point in origins[~blocked]:
        top = float(point[2]) - float(gap_mm)
        if top <= ground_z + 1.0:
            continue
        # 高柱必须加粗，否则耳尖那种 50mm 高的支撑一碰就倒
        height = top - float(ground_z)
        radius = max(float(base_mm), min(2.2, 0.9 + height * 0.02))
        columns.append(
            _tapered_column(
                (float(point[0]), float(point[1])), radius, tip_mm, float(ground_z), top
            )
        )

    if not columns:
        return None, {"count": 0, "reason": "悬垂面下方都有材料"}

    try:
        merged = trimesh.boolean.union(columns, engine="manifold")
    except Exception:
        merged = trimesh.util.concatenate(columns)

    return merged, {
        "count": len(columns),
        "tip_mm": tip_mm,
        "base_mm": base_mm,
        "gap_mm": gap_mm,
        "faces": int(len(merged.faces)),
    }


def _extrude_convex(polygon: Polygon, z0: float, z1: float) -> trimesh.Trimesh:
    """把凸多边形挤出成实体。凸多边形可以直接扇形三角化，不依赖三角化库。"""
    coords = np.asarray(polygon.exterior.coords)[:-1]
    if len(coords) < 3:
        raise ValueError("多边形点不足")

    # shapely 不保证环的方向；法线全靠绕序，先统一成逆时针
    signed = np.sum(
        coords[:, 0] * np.roll(coords[:, 1], -1) - np.roll(coords[:, 0], -1) * coords[:, 1]
    )
    if signed < 0:
        coords = coords[::-1]

    center = coords.mean(axis=0)
    n = len(coords)

    vertices = np.vstack(
        [
            np.column_stack([coords, np.full(n, z0)]),
            np.column_stack([coords, np.full(n, z1)]),
            [[center[0], center[1], z0]],
            [[center[0], center[1], z1]],
        ]
    )
    bot_c, top_c = 2 * n, 2 * n + 1

    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces.append([bot_c, j, i])              # 底面，法线朝下
        faces.append([top_c, n + i, n + j])      # 顶面，法线朝上
        faces.append([i, j, n + j])              # 侧面
        faces.append([i, n + j, n + i])
    return trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces))


def add_base_plate(
    mesh: trimesh.Trimesh,
    thickness_mm: float = 0.8,
    margin_mm: float = 1.0,
) -> tuple[trimesh.Trimesh, str | None]:
    """在底面加一块薄板，提升热床附着（小接触面积的首选补救）。"""
    low = mesh.bounds[0]
    band = mesh.vertices[mesh.vertices[:, 2] <= low[2] + max(thickness_mm, 0.2)]
    if len(band) < 3:
        return mesh, "底面顶点不足，跳过底板"

    hull = MultiPoint(band[:, :2]).convex_hull
    if hull.is_empty or hull.area <= 0:
        return mesh, "底面包络为空，跳过底板"
    if margin_mm > 0:
        hull = hull.buffer(margin_mm, join_style=2)
        hull = hull.convex_hull

    try:
        # 底板必须贴在模型当前底面上，不能直接放在 z=0：
        # 这时模型还没做最终平移，底面在负坐标区。
        plate = _extrude_convex(hull, float(low[2]), float(low[2] + thickness_mm))
        lifted = mesh.copy()
        lifted.apply_translation([0, 0, thickness_mm])
        merged = trimesh.boolean.union([lifted, plate], engine="manifold")
    except Exception as exc:
        return mesh, f"底板合并失败：{type(exc).__name__}"

    if merged is None or not merged.is_watertight:
        return mesh, "底板合并结果不水密，已回退"
    return merged, None


def inspect_print(
    mesh: trimesh.Trimesh,
    overhang_deg: float = 45.0,
    ray_samples: int = 600,
    layer_samples: int = 10,
    nozzle_mm: float = 0.4,
) -> dict:
    """可打印性体检。指标是估算值，用来发现明显问题，不是精确测量。"""
    normals = np.asarray(mesh.face_normals)
    areas = np.asarray(mesh.area_faces)
    total_area = float(areas.sum()) or 1e-12
    centers_z = np.asarray(mesh.triangles_center)[:, 2]
    low_z = float(mesh.bounds[0][2])

    bottom_mask = (centers_z < low_z + 0.15) & (normals[:, 2] < -0.5)
    contact_area = float(areas[bottom_mask].sum())
    footprint = _hull_area(mesh) or 1e-12
    contact_ratio = contact_area / footprint

    overhang_mask = (normals[:, 2] < -np.cos(np.radians(overhang_deg))) & (~bottom_mask)
    overhang_ratio = float(areas[overhang_mask].sum() / total_area)

    min_feature = None
    try:
        rng = np.random.default_rng(0)
        count = min(ray_samples, len(areas))
        picks = rng.choice(len(areas), size=count, replace=False, p=areas / areas.sum())
        points = np.asarray(mesh.triangles_center)[picks]
        dirs = normals[picks]
        origins = points - dirs * 1e-3
        intersector = trimesh.ray.ray_triangle.RayMeshIntersector(mesh)
        locations, ray_ids, _ = intersector.intersects_location(
            origins, -dirs, multiple_hits=False
        )
        if len(locations):
            distances = np.linalg.norm(locations - origins[ray_ids], axis=1)
            distances = distances[distances > 1e-3]
            if len(distances) >= 10:
                min_feature = float(np.percentile(distances, 5))
    except Exception:
        min_feature = None

    open_layers = 0
    try:
        z_low, z_high = mesh.bounds[:, 2]
        for k in range(1, layer_samples):
            z = z_low + (z_high - z_low) * k / layer_samples
            section = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
            if section is not None and not getattr(section, "is_closed", True):
                open_layers += 1
    except Exception:
        open_layers = -1

    result = {
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "face_count": int(len(mesh.faces)),
        "contact_area_mm2": round(contact_area, 2),
        "contact_ratio": round(float(contact_ratio), 3),
        "overhang_ratio": round(overhang_ratio, 3),
        "min_feature_mm": None if min_feature is None else round(min_feature, 2),
        "open_layers": open_layers,
    }

    warnings = []
    if not mesh.is_watertight:
        warnings.append("网格不水密，切片器可能拒绝或自行修补")
    if contact_ratio < 0.03:
        warnings.append(f"底面接触率仅 {contact_ratio:.1%}，建议加底板或换姿态")
    if min_feature is not None and min_feature < nozzle_mm * 2:
        warnings.append(
            f"最小特征约 {min_feature:.2f} mm，接近或低于 {nozzle_mm} mm 喷嘴的两倍，细部可能打不出来"
        )
    if overhang_ratio > 0.25:
        warnings.append(f"悬垂面积占比 {overhang_ratio:.0%}，切片时需要支撑")
    if len(mesh.faces) > 500_000:
        warnings.append("面数超过 50 万，切片会明显变慢，建议降低面数上限")
    if open_layers > 0:
        warnings.append(f"{open_layers} 层截面不闭合，模型可能存在自交或裂缝")

    result["warnings"] = warnings
    return result


def make_printable(
    mesh: trimesh.Trimesh,
    *,
    target_height_mm: float = 60.0,
    orientation: str = "canonical",
    floor_mode: str = "auto",
    floor_offset_mm: float = 0.0,
    floor_ratio: float = 0.16,
    base_plate_mm: float = 0.0,
    base_margin_mm: float = 1.0,
    max_faces: int | None = 120_000,
    smooth_iterations: int = 0,
    hollow_wall_mm: float = 0.0,
    wall_top_mm: float = 0.0,
    open_top_mm: float = 0.0,
    drain_mm: float = 0.0,
    drain_count: int = 1,
    supports: bool = False,
    support_angle_deg: float = 45.0,
    support_gap_mm: float = 0.2,
) -> tuple[trimesh.Trimesh, dict]:
    """把实体网格变成尺寸明确、底面平整、可切片的状态。"""
    steps: list[str] = []
    notes: list[str] = []
    work = mesh.copy()

    if orientation == "auto":
        transform, table = choose_orientation(work)
        work.apply_transform(transform)
        best = max(table, key=lambda item: item["score"])
        steps.append(f"姿态：朝下轴 {tuple(best['direction'])}，评分 {best['score']}")
    else:
        table = []
        steps.append("姿态：保持原始坐标（+Z 朝上）")

    extents = np.asarray(work.extents, dtype=float)
    if extents[2] <= 0:
        raise ValueError("网格高度为 0，无法归一化")
    scale = float(target_height_mm) / float(extents[2])
    work.apply_scale(scale)
    steps.append(f"缩放：×{scale:.4g}，高度 {target_height_mm} mm")

    # 平滑会让网格整体收缩，所以必须排在尺寸补偿之前，否则成品会偏矮
    if smooth_iterations > 0:
        try:
            trimesh.smoothing.filter_laplacian(work, iterations=int(smooth_iterations))
            steps.append(f"平滑 {smooth_iterations} 次")
        except Exception as exc:
            notes.append(f"平滑跳过：{type(exc).__name__}")

    z_cut = float(work.bounds[0][2])
    if floor_mode == "auto":
        z_cut = auto_floor_z(work, target_ratio=floor_ratio)
        steps.append(f"切底高度 {z_cut:.2f} mm（自动接触面积）")
    elif floor_mode == "offset":
        z_cut = z_cut + float(floor_offset_mm)
        steps.append(f"切底高度 {z_cut:.2f} mm（偏移 {floor_offset_mm} mm）")
    elif floor_mode == "flat":
        steps.append("不切底，直接落在 z=0")

    if floor_mode in ("auto", "offset"):
        cut = cut_below(work, z_cut)
        if cut is None:
            notes.append("布尔切底失败，改为整体下沉到 z=0（底面可能不平）")
        else:
            work = cut
            if not work.is_watertight:
                notes.append("切底后不水密，已体素重建")
                work, _ = voxel_solidify(work, resolution=200)

    # 简化放在尺寸补偿之前：二次误差简化也会让体积有微小变化
    if max_faces and len(work.faces) > max_faces:
        try:
            work = work.simplify_quadric_decimation(face_count=int(max_faces))
            steps.append(f"简化到 {max_faces} 面")
        except Exception as exc:
            notes.append(f"简化跳过：{type(exc).__name__}")

    # 切底会削掉一截，目标高度就不再等于成品高度。这里补一次缩放，
    # 让"本体高度"回到用户设定的值；底板厚度在之后叠加，单独另算。
    height_now = float(work.extents[2])
    if target_height_mm and height_now > 0:
        factor = float(target_height_mm) / height_now
        if abs(factor - 1.0) > 0.005:
            work.apply_scale(factor)
            steps.append(f"切底补偿 ×{factor:.4f}，本体高度回到 {target_height_mm} mm")

    hollow_meta: dict = {}
    if hollow_wall_mm and hollow_wall_mm > 0:
        try:
            work, hollow_meta = hollow_out(
                work,
                wall_mm=hollow_wall_mm,
                open_top_mm=open_top_mm,
                wall_top_mm=wall_top_mm if wall_top_mm > 0 else None,
            )
            if hollow_meta["wall_top_mm"] != hollow_meta["wall_mm"]:
                steps.append(
                    f"抽壳：壁厚 {hollow_meta['wall_mm']} → {hollow_meta['wall_top_mm']} mm"
                    f"（下厚上薄，体素 {hollow_meta['pitch_mm']} mm）"
                )
            else:
                steps.append(
                    f"抽壳：空腔壁厚约 {hollow_meta['wall_mm']} mm"
                    f"（体素 {hollow_meta['pitch_mm']} mm）"
                )
            if hollow_meta.get("open_top_mm"):
                steps.append(f"顶部开口 {hollow_meta['open_top_mm']} mm")
            if hollow_meta.get("open_top_failed"):
                notes.append("顶部开口失败，保留封闭壳体")
            before = len(work.faces)
            work = reduce_faces(work, max_faces)
            if len(work.faces) == before and max_faces and before > max_faces * 1.05:
                notes.append(
                    f"简化会破坏空心壳的水密性，已保留 {before:,} 面"
                    "（文件偏大但可正常切片）"
                )
        except Exception as exc:
            notes.append(f"抽壳失败：{exc}")
            hollow_meta = {}

    # 开口会削掉顶部，同样会让成品矮一截，再补一次
    height_now = float(work.extents[2])
    if target_height_mm and height_now > 0:
        factor = float(target_height_mm) / height_now
        if abs(factor - 1.0) > 0.005:
            work.apply_scale(factor)
            steps.append(f"开口补偿 ×{factor:.4f}，本体高度回到 {target_height_mm} mm")
            if hollow_meta:
                steps.append(
                    f"补偿后壁厚 {hollow_meta['wall_mm'] * factor:.2f}"
                    f" → {hollow_meta['wall_top_mm'] * factor:.2f} mm"
                )

    if base_plate_mm and base_plate_mm > 0:
        work, plate_note = add_base_plate(work, base_plate_mm, base_margin_mm)
        if plate_note:
            notes.append(plate_note)
        else:
            steps.append(f"底板 {base_plate_mm} mm，外扩 {base_margin_mm} mm")

    # 排水孔放在最后：要连底板一起打穿，否则树脂排不出去
    if drain_mm and drain_mm > 0:
        work, drain_meta = punch_drain(work, diameter_mm=drain_mm, count=drain_count)
        if drain_meta.get("error"):
            notes.append(f"排水孔跳过：{drain_meta['error']}")
        else:
            steps.append(
                f"排水孔 {drain_meta['holes']} 个 × "
                f"{drain_meta['diameter_mm']} mm（距中心 {drain_meta['ring_mm']} mm）"
            )

    bounds = work.bounds
    work.apply_translation(
        [-(bounds[0][0] + bounds[1][0]) / 2, -(bounds[0][1] + bounds[1][1]) / 2, -bounds[0][2]]
    )
    size = np.asarray(work.extents, dtype=float)
    steps.append("居中并把底面压到 z=0")

    # 支撑必须在落地之后生成：它要从悬垂面一路落到 z=0 这个热床平面。
    # 指标先算，避免支撑柱影响接触率、悬垂占比这些描述"零件本身"的数据。
    print_metrics = inspect_print(work)
    support_info: dict = {"enabled": bool(supports), "count": 0}
    if supports:
        support_mesh, support_info = make_supports(
            work,
            angle_deg=support_angle_deg,
            gap_mm=support_gap_mm,
            ground_z=float(base_plate_mm) if base_plate_mm > 0 else 0.0,
        )
        support_info["enabled"] = True
        if support_mesh is not None:
            steps.append(
                f"支撑：{support_info['count']} 根，顶端留 {support_gap_mm} mm 间隙"
                f"（柱顶 {support_info['tip_mm']} mm，柱底 {support_info['base_mm']} mm）"
            )
            work = trimesh.util.concatenate([work, support_mesh])
            notes.append("导出文件已包含支撑，打印后沿间隙掰断")
        else:
            notes.append(f"未生成支撑：{support_info.get('reason')}")

    report = {
        "params": {
            "target_height_mm": target_height_mm,
            "orientation": orientation,
            "floor_mode": floor_mode,
            "floor_offset_mm": floor_offset_mm,
            "base_plate_mm": base_plate_mm,
            "max_faces": max_faces,
            "smooth_iterations": smooth_iterations,
            "hollow_wall_mm": hollow_wall_mm,
            "wall_top_mm": wall_top_mm,
            "open_top_mm": open_top_mm,
            "drain_mm": drain_mm,
            "drain_count": drain_count,
            "supports": supports,
            "support_angle_deg": support_angle_deg,
            "support_gap_mm": support_gap_mm,
        },
        "supports": support_info,
        "steps": steps,
        "notes": notes,
        "orientation_scores": table,
        "mesh": mesh_status(work),
        "size_mm": {
            "x": round(float(size[0]), 2),
            "y": round(float(size[1]), 2),
            "z": round(float(size[2]), 2),
        },
        "print": print_metrics,
    }
    return work, report
