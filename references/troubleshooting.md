# 排错

## 云端

**`402 免费体验额度已耗尽，且未开启后付费`**
额度按模型独立计算。换一个模型重试：

```bash
./scripts/build.sh photo.jpg --model hy-3d-3.0
```

实测可用的回退顺序：`hy-3d-3.1` → `hy-3d-3.0` → `hy-3d-express`。
`tripo-*` 与 `rodin-*` 通常没有免费额度。

**`三个字段不能同时为空`**
图片只能走 `image_base64`。传 `file.url` 或 `image.url` 会被网关忽略。

**`ActionOffline`**
`ai3d.tencentcloudapi.com` 的旧接口已下线，代码走的是 `tokenhub.tencentmaas.com`。

**返回的模型躺着**
云端 glTF 是 Y 轴朝上，代码里已经转过一次。不要再转。

## 本地依赖

**装依赖卡住 / 超时**
默认源在部分网络下会静默卡死。`install.sh` 会自动回退到清华镜像；手动装时：

```bash
.venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**`No module named 'lxml'`**
读第三方 3MF 需要它，写 3MF 不需要（本项目自己写）。按需安装：

```bash
.venv/bin/python -m pip install lxml
```

**装了 rembg 之后 numpy 报错**
rembg 会把 numpy 拉到 2.x，和 pymeshlab 冲突。用 `WITH_REMBG=1 ./scripts/install.sh`，
它按 `requirements-optional.txt` 的固定版本装；已装坏就重建 venv。

## 几何

**简化后提示"保留原始面数"**
空心壳有两层曲面，普通简化会缝成非流形，代码会回退到拓扑约束简化。面数偏高是正确结果。

**底板加了但尺寸变了**
补偿顺序是：平滑 → 切底 → 简化 → 高度补偿 → 底板。底板是额外叠加的，
所以 `size.z` 会等于目标高度 + 底板厚度，报告里的数值以实测为准。

**镂空件底部积水（树脂打印）**
加 `--drain 3`，并用射线检查中心轴从底到顶是否贯通。

## 切片器

**Bambu Studio 提示"该 3MF 不是来自 Bambu Lab"**
正常。只加载几何和颜色，打印参数需要自己在切片器里设。

**导入后看不到窗口**
`open -a` 的返回值不代表窗口已就绪。用 `screencapture` 或直接看桌面确认，
不要只凭退出码判断成功。
