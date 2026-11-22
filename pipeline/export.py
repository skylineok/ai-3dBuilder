"""导出：STL 给切片器，3MF 带毫米单位，GLB 给网页预览。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import trimesh

__all__ = ["export_mesh", "write_3mf", "write_report"]

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""


def write_3mf(mesh: trimesh.Trimesh, path: str | Path, name: str = "model") -> Path:
    """写最小可用 3MF：单位毫米 + 单个对象。

    不用 trimesh 自带的 3MF 导出，是为了把 unit="millimeter" 明确写进去——
    切片器对这个字段的信任程度远高于 STL 的约定俗成。
    """
    path = Path(path)
    vertices = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces, dtype=np.int64)

    v_lines = [
        f'<vertex x="{v[0]:.4f}" y="{v[1]:.4f}" z="{v[2]:.4f}"/>' for v in vertices
    ]
    f_lines = [
        f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>' for f in faces
    ]
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        f'<resources><object id="1" type="model" name="{name}"><mesh>\n'
        f'<vertices>{"".join(v_lines)}</vertices>\n'
        f'<triangles>{"".join(f_lines)}</triangles>\n'
        '</mesh></object></resources>\n'
        '<build><item objectid="1"/></build>\n'
        '</model>'
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("3D/3dmodel.model", model)
    return path


def export_mesh(
    mesh: trimesh.Trimesh,
    out_dir: str | Path,
    stem: str = "model",
    formats: tuple[str, ...] = ("stl", "3mf", "glb"),
) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    for fmt in formats:
        target = out_dir / f"{stem}.{fmt}"
        if fmt == "3mf":
            write_3mf(mesh, target, name=stem)
        elif fmt == "glb":
            mesh.export(target, file_type="glb")
        else:
            mesh.export(target, file_type="stl")
        written[fmt] = str(target)
    return written


def write_report(report: dict, out_dir: str | Path, stem: str = "model") -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{stem}.report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 打印体检", ""]
    size = report.get("size_mm", {})
    lines.append(f"尺寸：{size.get('x')} × {size.get('y')} × {size.get('z')} mm")
    mesh_info = report.get("mesh", {})
    lines.append(f"网格：{mesh_info.get('faces')} 面，水密 {mesh_info.get('watertight')}")
    print_info = report.get("print", {})
    lines.append(
        f"接触率：{print_info.get('contact_ratio')}　悬垂占比：{print_info.get('overhang_ratio')}　"
        f"最小特征：{print_info.get('min_feature_mm')} mm"
    )
    if report.get("steps"):
        lines += ["", "## 处理步骤"] + [f"- {s}" for s in report["steps"]]
    if report.get("notes"):
        lines += ["", "## 备注"] + [f"- {s}" for s in report["notes"]]
    warnings = print_info.get("warnings") or []
    lines += ["", "## 风险"] + ([f"- {w}" for w in warnings] or ["- 未发现明显问题"])

    md_path = out_dir / f"{stem}.report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path)}
