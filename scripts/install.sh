#!/usr/bin/env bash
# 安装：建虚拟环境、装依赖、生成 .env、跑一次离线自检。
# 用法：./scripts/install.sh            默认源失败时自动切清华镜像
#       WITH_REMBG=1 ./scripts/install.sh  额外装抠图（+约 200MB）
set -euo pipefail

APP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

# macOS 自带的 python3 是 3.9，自动挑一个 3.10+ 的解释器
pick_python() {
  local cand
  for cand in "${PYTHON:-}" python3.12 python3.11 python3.10 python3; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null || continue
    printf '%s' "$cand"
    return 0
  done
  return 1
}

PY="$(pick_python)" || {
  echo "需要 Python 3.10 及以上。系统默认是 $(python3 -V 2>&1)，请安装 python3.11+ 或设置 PYTHON=/path/to/python3.11"
  exit 1
}

echo "[1/4] 创建虚拟环境 $APP/.venv"
"$PY" -m venv .venv
./.venv/bin/python -m pip install -q --upgrade pip

echo "[2/4] 安装依赖"
if ! ./.venv/bin/python -m pip install -q -r requirements.txt; then
  echo "      默认源失败，改用镜像 $MIRROR"
  ./.venv/bin/python -m pip install -q -r requirements.txt -i "$MIRROR"
fi

if [ "${WITH_REMBG:-0}" = "1" ]; then
  echo "      安装抠图组件（可选）"
  ./.venv/bin/python -m pip install -q -r requirements-optional.txt -i "$MIRROR" \
    || echo "      抠图组件安装失败，重建时会跳过抠图，不影响主流程"
fi

echo "[3/4] 准备 .env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "      已生成 .env。要生成三维模型，请在 .env 里填入 TENCENT_API_KEY。"
  echo "      只做透光浮雕（lithophane）不需要任何 key。"
fi

echo "[4/4] 离线自检（透光浮雕）"
./.venv/bin/python - <<'PY'
from pathlib import Path
from PIL import Image, ImageDraw

out = Path("outputs"); out.mkdir(exist_ok=True)
im = Image.new("L", (300, 400), 245)
d = ImageDraw.Draw(im)
d.ellipse((70, 60, 230, 220), fill=60)
d.rectangle((110, 220, 190, 370), fill=110)
d.text((130, 300), "SELF TEST", fill=230)
im.save(out / "_selftest.png")
PY
./.venv/bin/python cli.py --image outputs/_selftest.png --mode lithophane --width 80 \
  -o outputs/_selftest | tail -3

echo
echo "完成。下一步："
echo "  ./scripts/build.sh <图片> --mode lithophane    # 人像/照片，不需要 key"
echo "  ./scripts/build.sh <图片> --height 80          # 物体，需要 TENCENT_API_KEY"
