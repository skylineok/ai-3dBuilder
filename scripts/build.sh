#!/usr/bin/env bash
# 一条命令：图片 -> 可打印模型 -> 打开切片器。
# 所有路径都相对本脚本解析，可以在任何目录调用。
set -euo pipefail

APP="$(cd "$(dirname "$0")/.." && pwd)"
PY="$APP/.venv/bin/python"

IMAGE=""
NAME=""
HEIGHT=70
WIDTH=120
MODEL="hy-3d-3.1"
MODE="solid"
SUPPORTS=""
SLICER="BambuStudio"
DRY=0

usage() {
  cat <<'EOF'
用法: build.sh <图片路径> [选项]
  --mode <solid|lithophane>  默认 solid；人像/照片用 lithophane
  --name <名字>     输出目录名，默认取图片文件名
  --height <mm>     solid 模式目标高度，默认 70
  --width <mm>      lithophane 模式板宽，默认 120
  --model <slug>    云端模型，默认 hy-3d-3.1
  --supports        内嵌可掰断支撑（不加则交给切片器）
  --slicer <app>    切片器应用名，默认 BambuStudio，传 none 只导文件
  --dry-run         只打印将要执行的命令
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --height) HEIGHT="$2"; shift 2 ;;
    --width) WIDTH="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --supports) SUPPORTS="--supports"; shift ;;
    --slicer) SLICER="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) IMAGE="$1"; shift ;;
  esac
done

[[ -n "$IMAGE" ]] || { usage; exit 1; }
[[ -f "$IMAGE" ]] || { echo "找不到图片：$IMAGE" >&2; exit 1; }
[[ -x "$PY" ]] || { echo "未安装，先运行 $APP/scripts/install.sh" >&2; exit 1; }

IMAGE="$(cd "$(dirname "$IMAGE")" && pwd)/$(basename "$IMAGE")"
NAME="${NAME:-$(basename "${IMAGE%.*}")}"
OUT="outputs/$NAME"

if [[ "$MODE" == "lithophane" ]]; then
  CMD=("$PY" cli.py --image "$IMAGE" --mode lithophane --width "$WIDTH" -o "$OUT")
else
  CMD=("$PY" cli.py --image "$IMAGE" --provider tencent --model "$MODEL"
       --height "$HEIGHT" --smooth 3 --base 1.2 -o "$OUT")
  [[ -n "$SUPPORTS" ]] && CMD+=("$SUPPORTS")
fi

if [[ "$DRY" == "1" ]]; then
  echo "cd $APP"
  printf '%q ' "${CMD[@]}"; echo
  [[ "$SLICER" != "none" ]] && echo "open -a $SLICER $APP/$OUT/model.3mf"
  exit 0
fi

cd "$APP"
"${CMD[@]}"

MODEL_FILE="$APP/$OUT/model.3mf"
[[ -f "$MODEL_FILE" ]] || MODEL_FILE="$APP/$OUT/model.stl"

if [[ "$SLICER" != "none" && -d "/Applications/$SLICER.app" ]]; then
  open -a "$SLICER" "$MODEL_FILE"
  echo "已发送到 $SLICER：$MODEL_FILE"
elif [[ "$SLICER" != "none" ]] && command -v "$SLICER" >/dev/null 2>&1; then
  "$SLICER" "$MODEL_FILE"
  echo "已发送到 $SLICER：$MODEL_FILE"
else
  echo "模型：$MODEL_FILE"
fi
