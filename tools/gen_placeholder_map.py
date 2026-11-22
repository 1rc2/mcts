"""生成内置「示例底图」占位图（1536x1024，调色板 PNG，体积小）。

这张图只用于演示：它不是任何游戏/库街区的真实地图。点位数据里的 x/y 是
0~1 归一化坐标，因此换底图不需要改坐标——但底图必须与点位数据同源，
否则点位会整体偏移（见 README「数据导入」与 resources/README_data.md）。

用法：
    cd 项目根目录 && python3 tools/gen_placeholder_map.py
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "src" / "kumap" / "resources" / "map" / "world_map.png"
W, H = 1536, 1024
SEED = 20261121

# 与 points.json 的 4x2 区域划分保持一致
REGIONS = ["荒石坡", "中曲台地", "虎口山脉", "残息海岸",
           "青栎庭院", "乘霄山", "回音峡", "今州城郊"]

# 底图上的中文标注需要 CJK 字体；找不到就退回 ASCII 标签，不影响功能
_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def _font(size: int):
    for p in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _land_mask(rng: random.Random) -> list[list[bool]]:
    """用几个随机大 blob 拼出大陆轮廓（纯装饰，让缩放到极限也不是一色）。"""
    grid = [[False] * W for _ in range(H)]
    blobs = []
    for _ in range(26):
        blobs.append((rng.uniform(0, W), rng.uniform(0, H), rng.uniform(90, 300)))
    step = 4
    for y in range(0, H, step):
        for x in range(0, W, step):
            inside = False
            for cx, cy, r in blobs:
                dx, dy = x - cx, y - cy
                wob = 1.0 + 0.22 * math.sin(math.atan2(dy, dx) * 3.0 + cx * 0.01)
                if dx * dx + dy * dy < (r * wob) ** 2:
                    inside = True
                    break
            if inside:
                for yy in range(y, min(H, y + step)):
                    for xx in range(x, min(W, x + step)):
                        grid[yy][xx] = True
    return grid


def build() -> Image.Image:
    rng = random.Random(SEED)
    img = Image.new("RGB", (W, H), (28, 40, 56))          # 海
    dr = ImageDraw.Draw(img, "RGBA")
    mask = _land_mask(rng)

    # 陆地：按到边缘距离分两档上色，制造海岸线
    for y in range(H):
        row = mask[y]
        x = 0
        while x < W:
            if row[x]:
                x0 = x
                while x < W and mask[y][x]:
                    x += 1
                dr.rectangle([x0, y, x - 1, y], fill=(58, 84, 66))
            else:
                x += 1
    for _ in range(140):                                    # 高地斑块
        cx, cy = rng.uniform(0, W), rng.uniform(0, H)
        if not mask[int(cy)][int(cx)]:
            continue
        r = rng.uniform(18, 90)
        dr.ellipse([cx - r, cy - r * 0.62, cx + r, cy + r * 0.62],
                   fill=(84, 110, 84, 190))
    for _ in range(60):                                     # 山脉三角
        cx, cy = rng.uniform(0, W), rng.uniform(0, H)
        if not mask[int(cy)][int(cx)]:
            continue
        r = rng.uniform(10, 34)
        dr.polygon([(cx, cy - r), (cx - r * 0.9, cy + r * 0.5), (cx + r * 0.9, cy + r * 0.5)],
                   fill=(140, 148, 138, 210))
    for _ in range(70):                                     # 水系
        cx, cy = rng.uniform(0, W), rng.uniform(0, H)
        if not mask[int(cy)][int(cx)]:
            continue
        pts = [(cx, cy)]
        for i in range(9):
            cx += rng.uniform(-26, 34)
            cy += rng.uniform(-20, 26)
            pts.append((cx, cy))
        dr.line(pts, fill=(70, 120, 160, 200), width=max(2, rng.randint(2, 6)))

    # 网格（方便肉眼对齐点位）
    for i in range(1, 12):
        x = W * i / 12
        dr.line([(x, 0), (x, H)], fill=(255, 255, 255, 26), width=1)
    for i in range(1, 8):
        y = H * i / 8
        dr.line([(0, y), (W, y)], fill=(255, 255, 255, 26), width=1)

    # 区域划分与名称（4x2，与 points.json 的 bbox 一致）
    f_region, f_note = _font(26), _font(20)
    for idx in range(8):
        col, row = idx % 4, idx // 4
        x0, y0 = col * (W // 4), row * (H // 2)
        dr.rectangle([x0, y0, x0 + W // 4 - 1, y0 + H // 2 - 1],
                     outline=(255, 255, 255, 60), width=2)
        dr.text((x0 + 14, y0 + 10), f"{REGIONS[idx]}  r{idx + 1}",
                font=f_region, fill=(235, 240, 245, 210))

    dr.text((16, H - 34), "示例占位底图 · 非官方地图 · 请用同源导出图替换",
            font=f_note, fill=(235, 240, 245, 200))
    return img


def main() -> None:
    img = build()
    pal = img.quantize(colors=256, method=Image.MEDIANCUT)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp.png")
    pal.save(tmp, format="PNG", optimize=True)
    tmp.replace(OUT)
    print(f"written {OUT} {OUT.stat().st_size / 1024:.1f} KB {pal.size[0]}x{pal.size[1]}")


if __name__ == "__main__":
    main()
