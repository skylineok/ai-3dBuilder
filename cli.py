"""图片 -> 可打印 3D 模型的命令行入口。

两条分支：
  --mode solid      云端重建 -> 修复 -> 摆正 -> 定尺寸 -> 可选抽壳 -> 支撑 -> 导出
  --mode lithophane 照片亮度 -> 板厚 -> 带边框实体，不依赖云端
用 --mesh 可以把任意已有网格直接送进后处理链。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from pipeline.cloud import CloudError
from pipeline.export import export_mesh, write_report
from pipeline.printable import make_printable
from pipeline.reconstruct import load_input, reconstruct
from pipeline.repair import make_solid

ROOT = Path(__file__).resolve().parent
try:
    __version__ = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    __version__ = "0.01"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="图片/网格 -> 可打印 STL")
    parser.add_argument("--version", action="version", version=f"ai-3d-builder {__version__}")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", help="输入图片（需要重建 provider）")
    src.add_argument("--mesh", help="输入已有网格，跳过重建")
    parser.add_argument("--provider", default="tencent", help="重建后端：tencent / replicate / fal")
    parser.add_argument("--model", default="", help="云端模型 slug，如 hy-3d-3.1")
    parser.add_argument("--cloud-inputs", default="{}", help="云端额外参数，JSON 字符串")
    parser.add_argument("-o", "--out", default="outputs/run", help="输出目录")
    parser.add_argument("--stem", default="model", help="输出文件名前缀")

    parser.add_argument("--height", type=float, default=60.0, help="目标高度 mm")
    parser.add_argument(
        "--orientation",
        default="canonical",
        choices=["canonical", "auto"],
        help="canonical 保持原始 +Z 朝上（重建结果和多数 STL 都正确），auto 按平整面自动判断",
    )
    parser.add_argument(
        "--floor",
        default="auto",
        choices=["auto", "flat", "offset"],
        help="切底方式：auto 按接触面积自动定高，flat 不切只落地，offset 固定切深",
    )
    parser.add_argument("--floor-offset", type=float, default=0.0, help="offset 模式的切深 mm")
    parser.add_argument("--base", type=float, default=0.0, help="底板厚度 mm，0 表示不加")
    parser.add_argument("--max-faces", type=int, default=120_000)
    parser.add_argument("--hollow", type=float, default=0.0, help="抽壳壁厚 mm，0 表示实心")
    parser.add_argument("--wall-top", type=float, default=0.0, help="顶部壁厚 mm，形成下厚上薄，0 表示等壁厚")
    parser.add_argument("--open-top", type=float, default=0.0, help="顶部开口深度 mm")
    parser.add_argument("--drain", type=float, default=0.0, help="底部排水孔直径 mm，0 表示不开")
    parser.add_argument("--drain-count", type=int, default=1, help="排水孔数量")
    parser.add_argument("--supports", action="store_true", help="生成可掰断的悬垂支撑")
    parser.add_argument("--support-angle", type=float, default=45.0, help="判定悬垂的角度阈值")
    parser.add_argument("--support-gap", type=float, default=0.2, help="支撑顶端与模型的间隙 mm")
    parser.add_argument("--voxel-resolution", type=int, default=160)
    parser.add_argument("--smooth", type=int, default=0)

    parser.add_argument("--formats", default="stl,3mf,glb")
    parser.add_argument("--mode", choices=["solid", "lithophane"], default="solid",
                        help="solid 走三维重建；lithophane 用照片亮度做透光浮雕")
    parser.add_argument("--width", type=float, default=120.0, help="浮雕板宽度 mm")
    parser.add_argument("--thick-min", type=float, default=0.8, help="浮雕最薄处 mm")
    parser.add_argument("--thick-max", type=float, default=3.2, help="浮雕最厚处 mm")
    parser.add_argument("--frame", type=float, default=8.0, help="浮雕边框宽度 mm")
    parser.add_argument("--frame-height", type=float, default=4.0, help="浮雕边框凸起高度 mm")
    parser.add_argument("--litho-grid", type=int, default=400, help="浮雕采样列数")
    return parser


def _run_lithophane(args) -> int:
    """透光浮雕：跳过三维重建，用照片亮度直接生成厚度场。"""
    from pipeline.export import write_3mf
    from pipeline.relief import make_lithophane

    if not args.image:
        print("[浮雕模式] 需要 --image", file=sys.stderr)
        return 2

    mesh, info = make_lithophane(
        args.image,
        width_mm=args.width,
        min_mm=args.thick_min,
        max_mm=args.thick_max,
        frame_mm=args.frame,
        frame_height_mm=args.frame_height,
        max_grid=args.litho_grid,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "stl": str(out_dir / f"{args.stem}.stl"),
        "3mf": str(out_dir / f"{args.stem}.3mf"),
    }
    mesh.export(files["stl"], file_type="stl")
    write_3mf(mesh, files["3mf"], name="lithophane")

    tips = [
        "竖直打印：板面垂直于热床，厚度方向保持水平",
        "填充 100%，白色或浅色 PLA，层高 0.16–0.2mm",
        "底部加 5–10mm brim 防倾倒，不需要支撑",
        "从有起伏的一面朝光观看，背面是镜像",
    ]
    report = {"mode": "lithophane", **info, "print": {"warnings": [], "tips": tips}}
    (out_dir / f"{args.stem}.report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 透光浮雕",
        "",
        f"- 尺寸：{info['width_mm']} × {info['height_mm']} × {info['thickness_mm']} mm",
        f"- 板厚范围：{args.thick_min}–{args.thick_max} mm",
        f"- 面数：{info['faces']}，水密：{info['watertight']}",
        "",
        "## 打印要点",
        "",
    ] + [f"- {t}" for t in tips]
    (out_dir / f"{args.stem}.report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"尺寸：{info['width_mm']} × {info['height_mm']} × {info['thickness_mm']} mm")
    print(f"输出：{json.dumps(files, ensure_ascii=False)}")
    for tip in tips:
        print(f"[提示] {tip}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.mode == "lithophane":
        return _run_lithophane(args)

    if args.image:
        started = time.time()
        try:
            kwargs = {"inputs": json.loads(args.cloud_inputs)}
            if args.model:
                kwargs["model"] = args.model
            mesh = reconstruct(args.image, provider=args.provider, **kwargs)
        except CloudError as exc:
            print(f"[云端失败] {exc}", file=sys.stderr)
            return 3
        print(f"重建完成：{time.time() - started:.1f}s")
    else:
        mesh = load_input(args.mesh)

    print(f"输入：{len(mesh.faces)} 面，水密 {mesh.is_watertight}")

    out_dir = Path(args.out)
    if args.image:
        # 存一份原始重建结果：之后调高度/切底/面数都不用再等推理
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = out_dir / f"{args.stem}.raw.glb"
        mesh.export(raw_path, file_type="glb")
        print(f"原始网格：{raw_path}（可用 --mesh 直接复跑后处理）")

    solid, repair_info = make_solid(
        mesh,
        voxel_resolution=args.voxel_resolution,
        max_faces=args.max_faces,
        smooth_iterations=0,
    )
    print(f"修复：{repair_info['faces']} 面，水密 {repair_info['watertight']}")
    for note in repair_info["notes"]:
        print(f"  - {note}")

    printable, report = make_printable(
        solid,
        target_height_mm=args.height,
        orientation=args.orientation,
        floor_mode=args.floor,
        floor_offset_mm=args.floor_offset,
        base_plate_mm=args.base,
        max_faces=args.max_faces,
        smooth_iterations=args.smooth,
        hollow_wall_mm=args.hollow,
        wall_top_mm=args.wall_top,
        open_top_mm=args.open_top,
        drain_mm=args.drain,
        drain_count=args.drain_count,
        supports=args.supports,
        support_angle_deg=args.support_angle,
        support_gap_mm=args.support_gap,
    )
    report["repair"] = repair_info

    formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())
    files = export_mesh(printable, out_dir, stem=args.stem, formats=formats)
    files.update(write_report(report, out_dir, stem=args.stem))

    size = report["size_mm"]
    print(f"尺寸：{size['x']} × {size['y']} × {size['z']} mm")
    print(f"输出：{json.dumps(files, ensure_ascii=False)}")
    for warning in report["print"]["warnings"]:
        print(f"[风险] {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
