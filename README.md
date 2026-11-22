# ai-3d-builder

**一张照片进，一个能直接打印的 3D 文件出。**

输入一张图片，输出带尺寸、水密、可切片的 STL / 3MF / GLB，并可直接在 Bambu Studio 里打开。
两条流水线、一条命令、不需要本地显卡。

当前版本：**0.1.0** ｜ 许可证：[MIT](LICENSE)

---

## 它做什么

| 输入 | 走哪条路 | 产出 |
| --- | --- | --- |
| 玩具、手办、雕塑、产品、单只动物 | `--mode solid`（默认） | 云端单图重建出的实体模型，可抽壳、加底板、加支撑 |
| 人像、合影、照片、平面画作 | `--mode lithophane` | 透光浮雕板 + 边框，厚度随照片亮度变化 |
| 已有的坏网格（破面、法线翻转、比例不对） | `--mesh` | 修复、摆正、定尺寸后重新导出 |

实心模式调用腾讯云 MaaS 的三维生成接口（默认 `hy-3d-3.1`），**需要腾讯云 API Key**，一次调用约 2–4 分钟；
浮雕模式是纯本地的可选分支，约 10 秒。

## 两种模式怎么选

判断标准只有一句：**你要的是"像照片里的那个人/那张图"，还是"有这个物体的形状"？**

- 像照片 → `lithophane`。人脸的信息在明暗里，不在深度里。
- 有形状 → `solid`。单图重建看不到背面，但它能给出占空间的形体。

反例请务必避开：用 `solid` 跑人像，脸会糊、手指会粘成一块；用深度图去做浮雕，得到的是一个没有五官的鼓包。

## 快速开始

要求：macOS 或 Linux、Python 3.10+、能访问外网（`install.sh` 在默认源失败时会自动切清华镜像）。

```bash
git clone https://github.com/skylineok/ai-3dBuilder.git ai-3d-builder
cd ai-3d-builder
./scripts/install.sh
```

安装脚本会创建 `.venv`、安装依赖、生成 `.env`，最后跑一次离线自检——自检通过说明几何计算这一套是好的。

使用前必须在 `.env` 里填入腾讯云 MaaS 的 API Key，三维生成接口需要它：

```bash
TENCENT_API_KEY=sk-...
```

Key 只从环境变量或 `.env` 读取，代码里没有任何硬编码，`.env` 已在 `.gitignore` 中。

第一条命令：

```bash
./scripts/build.sh cat.jpg --height 70                            # 实心，70mm 高
./scripts/build.sh portrait.jpg --mode lithophane --width 120     # 浮雕，120mm 宽
```

成功后会直接把 `outputs/<图片名>/model.3mf` 送到 Bambu Studio。
没装 Bambu Studio 就加 `--slicer none`，只导出文件。

## 实心模式

```bash
./scripts/build.sh object.jpg --height 80 [--model hy-3d-3.1] [--supports] [--slicer none]
```

流程：云端重建 → 修复缝合 → 摆正切底 → 缩放到目标高度 → 可选抽壳/开孔/排水孔 → 可选支撑 → 导出。
重建结果会额外存一份 `model.raw.glb`，之后调参数用 `--mesh` 复跑，不用再花一次云端调用。

常用参数（直接透传给 `cli.py`）：

| 参数 | 作用 |
| --- | --- |
| `--hollow 1.4` | 抽壳，壁厚 1.4mm，适合花瓶、灯罩这类轻量件 |
| `--wall-top 1.2` | 下厚上薄的变壁厚，顶部 1.2mm |
| `--open-top 4` | 顶部开 4mm 深的开口，把密封花瓶改成容器 |
| `--drain 3` | 底部开直径 3mm 的垂直排水孔 |
| `--base 1.2` | 加 1.2mm 底板，增加热床接触面积 |
| `--smooth 3` | 拉普拉斯平滑，超过 5 次会明显磨掉细节 |
| `--supports` | 生成可掰断的内嵌支撑；不加则交给切片器生成 |

交付前应该核对三件事：`watertight` 为 true、`size.z` 等于目标高度、`print.warnings` 为空。
热床接触面积低于 3% 就加底板；最薄处低于 0.8mm 就整体放大。

## 透光浮雕模式

```bash
./scripts/build.sh portrait.png --mode lithophane --width 120
```

厚度映射亮度：暗的地方厚、亮的地方薄，四周带一圈凸起边框。全过程本地完成。

这几个打印参数不是建议，是必须：

- **竖直打印**，板面垂直于热床，让厚度方向保持水平
- **100% 填充**，白色或浅色 PLA，层高 0.16–0.2mm
- 底部加 **5–10mm brim** 防倾倒，**不需要支撑**
- 从有起伏的一面朝光看，背面是镜像

验收：`watertight` 为 true，最薄处不低于 0.6mm。

## 修一个已有网格

```bash
.venv/bin/python cli.py --mesh broken.stl --height 80 --base 1.2 -o outputs/fixed
```

不重建，只走后处理链：焊点、丢掉游离壳、补洞，洞太大补不上就退回体素重建，然后摆正、定尺寸、可选抽壳、导出。
耗时是秒级。体素回退能保证水密，但会磨平细节——报告里的 `repair.notes` 会写明实际走了哪条路。

## 输出文件

`outputs/<名字>/` 下：

| 文件 | 内容 |
| --- | --- |
| `model.stl` | 通用网格，任何切片器都能读 |
| `model.3mf` | 带毫米单位，优先用这个 |
| `model.glb` | 带材质，用来看效果 |
| `model.report.json` | 尺寸、面数、水密、热床接触、悬垂占比、警告 |
| `model.report.md` | 上面这份报告的可读版 |

## 云端模型与免费额度

`--model` 可选 `hy-3d-3.1`、`hy-3d-3.0`、`hy-3d-express`。

免费额度是**按模型**计的：某个模型返回 `402` 不代表 key 失效，换一个模型通常就能继续跑。
建议顺序 `hy-3d-3.1 → hy-3d-3.0 → hy-3d-express`，画质依次下降。细节见 [references/troubleshooting.md](references/troubleshooting.md)。

## 已验证的例子

| 输入 | 模式 | 结果 |
| --- | --- | --- |
| 猫咪照片 | solid | 94.1 × 53.1 × 101.2 mm，119,502 面，水密，Bambu Studio 读回一致 |
| 人像照片 | lithophane | 120mm 宽浮雕板，五官清晰 |
| 奥特曼、骑马武士 | solid | 带底板，热床接触充足，可直接切片 |

## 已知限制

- 单图重建会"猜"看不到的部分，所以手会粘、背面会平滑、正脸偏通用。这是输入决定的，不是参数能调的。
- 抽壳后的模型精简容易破坏水密，代码会退回保拓扑的精简器，面数可能停在 12 万以上。这是正确行为。
- Bambu Studio 导入通用 3MF 会提示"不是 Bambu Lab 导出，仅保留几何和颜色"，属正常提示。
- 自动打开切片器依赖 macOS 的 `open -a`；其他系统请加 `--slicer none` 后手动打开。

## 目录结构

```
ai-3d-builder/
├── SKILL.md              # 给 AI Agent 用的技能说明（也可当速查手册）
├── cli.py                # 命令行入口，--mode solid / lithophane / --mesh
├── pipeline/
│   ├── reconstruct.py    # 图片 -> 网格（腾讯云 / Replicate / fal）
│   ├── repair.py         # 焊接、补洞、体素回退、精简
│   ├── printable.py      # 摆正、定尺寸、抽壳、开孔、支撑、底板
│   ├── relief.py         # 透光浮雕网格
│   ├── export.py         # STL / 3MF / GLB / 报告
│   ├── tencent.py        # MaaS 提交与轮询
│   └── cloud.py          # 通用云端调用与下载
├── scripts/
│   ├── install.sh        # 安装 + 离线自检
│   └── build.sh          # 一条命令跑完并打开切片器
├── references/
│   └── troubleshooting.md
├── requirements.txt
├── VERSION
└── LICENSE
```

## 当作 Agent Skill 使用

本仓库本身就是一份 Skill：`SKILL.md` 里写了模式判断规则、参数选择、验收标准和交接方式。
把整个目录放进你的 Agent skills 目录即可被识别，目录名与 `name` 字段一致（`ai-3d-builder`）。

## 许可证

[MIT](LICENSE)
