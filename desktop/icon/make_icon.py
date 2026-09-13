"""FishCloud 桌面图标生成器。

品牌基线（见 ``docs/fishcloud-design.md`` §一）：朱砂印章方块
（渐变 #C74B3A → #B03A2E → #A53226，描边 #8B2A1F）+ 暖白鱼形（#F7F4EE）。
桌面图标在此之上叠一层更深的云形水印，让「鱼 + 云」两个语义同框。

产出（均落在本目录）：
* ``fishcloud.ico`` —— Windows 多尺寸图标（16/24/32/48/64/128/256）
* ``fishcloud-256.png`` / ``fishcloud-64.png`` —— README 与文档用位图
* ``fishcloud-icon.svg`` 由本脚本同步写出（与位图同几何，便于后续放大再绘）

几何全部在 1000×1000 画布内定义，先 4 倍超采样再降采样，保证小尺寸边缘干净。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
CANVAS = 1000
SS = 4  # 超采样倍数

# ── 品牌色 ───────────────────────────────────────────────────────────────
ZHU_TOP = (199, 75, 58)      # #C74B3A
ZHU_MID = (176, 58, 46)      # #B03A2E
ZHU_BOTTOM = (165, 50, 38)   # #A53226
EDGE = (139, 42, 31)         # #8B2A1F 印章描边
CREAM = (247, 244, 238)      # #F7F4EE 暖白
CLOUD_SCRIM = (120, 32, 24)  # 云形水印（叠在红底上的更暗红）

RADIUS = 0.16  # 圆角占边长比例（印章方块，比 iOS 圆角更收敛）

# ── 鱼形几何（取自 favicon.svg 的 24 网格，向右游动） ─────────────────────
FISH_PATH = {
    "body_top": ((4.6, 12.0), (12.0, 5.6), (20.2, 12.0)),
    "body_bottom": ((20.2, 12.0), (12.0, 18.4), (4.6, 12.0)),
    "tail": ((5.3, 12.0), (2.0, 8.9), (2.0, 15.1)),
    "eye_center": (16.65, 11.2),
    "eye_radius": 1.05,
}
FISH_SPAN = 18.2  # 含尾的横向跨度（24 网格单位）


def quad_points(p0, p1, p2, steps: int = 96):
    """把二次贝塞尔曲线采样成折线点列。"""
    points = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]
        points.append((x, y))
    return points


def fish_transform(width_ratio: float, center_y: float):
    """返回把 24 网格鱼形映射到 1000 画布的变换函数。

    Args:
        width_ratio: 鱼身（含尾）宽度占画布的比例。
        center_y: 鱼形竖直中心位置（画布比例）。
    """
    unit = CANVAS * width_ratio / FISH_SPAN
    fish_w = FISH_SPAN * unit
    left = (CANVAS - fish_w) / 2
    fish_h = 12.8 * unit
    top = CANVAS * center_y - fish_h / 2 - (12.0 - 5.6) * unit

    def to_canvas(pt):
        return (left + (pt[0] - 2.0) * unit, top + pt[1] * unit)

    return to_canvas, unit


def rounded_mask(size: int, radius_ratio: float) -> Image.Image:
    """圆角方块遮罩（灰度图，255 = 实心）。"""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * radius_ratio), fill=255
    )
    return mask


def gradient_tile(size: int) -> Image.Image:
    """朱砂渐变方块（印章底色）。"""
    top, mid, bottom = ZHU_TOP, ZHU_MID, ZHU_BOTTOM
    column = Image.new("RGB", (1, size))
    pixels = column.load()
    for y in range(size):
        t = y / (size - 1)
        if t <= 0.58:
            k = t / 0.58
            color = tuple(round(top[i] + (mid[i] - top[i]) * k) for i in range(3))
        else:
            k = (t - 0.58) / 0.42
            color = tuple(round(mid[i] + (bottom[i] - mid[i]) * k) for i in range(3))
        pixels[0, y] = color
    return column.resize((size, size), Image.NEAREST)


def cloud_mask(size: int, width_ratio: float, center_y: float) -> Image.Image:
    """云形遮罩：三个交叠圆 + 底边直条（云朵轮廓）。"""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    width = size * width_ratio
    left = (size - width) / 2
    base_y = size * center_y
    unit = width / 5.4
    # 三个云团（左小、中大、右中）与底座矩形
    draw.ellipse(
        (left + unit * 0.55, base_y - unit * 1.55, left + unit * 2.35, base_y + unit * 0.25),
        fill=255,
    )
    draw.ellipse(
        (left + unit * 1.55, base_y - unit * 2.45, left + unit * 3.75, base_y + unit * 0.25),
        fill=255,
    )
    draw.ellipse(
        (left + unit * 3.05, base_y - unit * 1.75, left + unit * 4.85, base_y + unit * 0.25),
        fill=255,
    )
    draw.rounded_rectangle(
        (left + unit * 0.6, base_y - unit * 0.75, left + unit * 4.8, base_y + unit * 0.25),
        radius=int(unit * 0.5),
        fill=255,
    )
    return mask


def render(px: int, variant: str = "merged") -> Image.Image:
    """渲染指定边长的图标位图（内部按 SS 倍超采样）。

    Args:
        px: 输出边长。
        variant: 云形处理方式——
            ``merged``：暖白云与鱼连成一个剪影（默认，识别度最高）；
            ``seal``：不放云，回到印章 + 鱼的纯品牌标记；
            ``float``：云悬浮在鱼上方，用更深一档的红做衬底。
    """
    size = px * SS
    tile = gradient_tile(size)
    mask = rounded_mask(size, RADIUS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(tile, (0, 0), mask)

    # 顶部内高光：模拟印章的釉面受光
    gloss = Image.new("L", (size, size), 0)
    gloss_draw = ImageDraw.Draw(gloss)
    gloss_draw.rounded_rectangle(
        (0, 0, size - 1, int(size * 0.52)), radius=int(size * RADIUS), fill=34
    )
    gloss = gloss.filter(ImageFilter.GaussianBlur(size * 0.02))
    canvas.paste(Image.new("RGBA", (size, size), (255, 245, 232, 255)), (0, 0), gloss)

    draw = ImageDraw.Draw(canvas)
    if variant == "float":
        # 云形水印：更深一档的红，浮在鱼上方、互不重叠
        cloud = cloud_mask(size, 0.52, 0.30)
        canvas.paste(Image.new("RGBA", (size, size), CLOUD_SCRIM + (92,)), (0, 0), cloud)
        fish_center = 0.63
    elif variant == "merged":
        # 云与鱼同色，上下相接后合成一个暖白剪影
        cloud = cloud_mask(size, 0.55, 0.42)
        canvas.paste(Image.new("RGBA", (size, size), CREAM + (255,)), (0, 0), cloud)
        fish_center = 0.615
    else:  # seal
        fish_center = 0.55

    # 鱼形（暖白）：主体两段贝塞尔 + 尾鳍三角
    to_canvas, unit = fish_transform(width_ratio=0.66, center_y=fish_center)
    body = quad_points(*FISH_PATH["body_top"]) + quad_points(*FISH_PATH["body_bottom"])
    draw.polygon([to_canvas(p) for p in body], fill=CREAM)
    draw.polygon([to_canvas(p) for p in FISH_PATH["tail"]], fill=CREAM)

    # 鱼眼：印章底色挖空
    eye_x, eye_y = to_canvas(FISH_PATH["eye_center"])
    eye_r = FISH_PATH["eye_radius"] * unit
    draw.ellipse((eye_x - eye_r, eye_y - eye_r, eye_x + eye_r, eye_y + eye_r), fill=ZHU_MID)

    # 印章描边
    stroke = max(2, int(size * 0.012))
    draw.rounded_rectangle(
        (stroke / 2, stroke / 2, size - 1 - stroke / 2, size - 1 - stroke / 2),
        radius=int(size * RADIUS),
        outline=EDGE + (150,),
        width=stroke,
    )

    return canvas.resize((px, px), Image.LANCZOS)


def svg_source() -> str:
    """与位图同几何的矢量源（便于后续放大再绘与文档引用）。"""
    to_canvas, unit = fish_transform(width_ratio=0.66, center_y=0.56)
    (bx0, by0), (bcx, bcy), (bx1, by1) = FISH_PATH["body_top"]
    (_, _, _) = FISH_PATH["body_bottom"][0], None, None
    tx = [to_canvas(p) for p in FISH_PATH["tail"]]
    p0, c, p1 = to_canvas((bx0, by0)), to_canvas((bcx, bcy)), to_canvas((bx1, by1))
    _, c2, _ = FISH_PATH["body_bottom"]
    c2p = to_canvas(c2)
    eye_x, eye_y = to_canvas(FISH_PATH["eye_center"])
    eye_r = FISH_PATH["eye_radius"] * unit
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000" width="1000" height="1000">
  <defs>
    <linearGradient id="seal" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#C74B3A"/>
      <stop offset="0.58" stop-color="#B03A2E"/>
      <stop offset="1" stop-color="#A53226"/>
    </linearGradient>
    <clipPath id="tile"><rect width="1000" height="1000" rx="160"/></clipPath>
  </defs>
  <g clip-path="url(#tile)">
    <rect width="1000" height="1000" fill="url(#seal)"/>
    <g fill="#782018" opacity="0.36">
      <ellipse cx="310" cy="272" rx="90" ry="95"/>
      <ellipse cx="470" cy="215" rx="110" ry="120"/>
      <ellipse cx="628" cy="262" rx="90" ry="100"/>
      <rect x="300" y="250" width="340" height="100" rx="50"/>
    </g>
    <path fill="#F7F4EE" d="M {p0[0]:.1f} {p0[1]:.1f} Q {c[0]:.1f} {c[1]:.1f} {p1[0]:.1f} {p1[1]:.1f}
             Q {c2p[0]:.1f} {c2p[1]:.1f} {p0[0]:.1f} {p0[1]:.1f} Z"/>
    <path fill="#F7F4EE" d="M {tx[0][0]:.1f} {tx[0][1]:.1f} L {tx[1][0]:.1f} {tx[1][1]:.1f} L {tx[2][0]:.1f} {tx[2][1]:.1f} Z"/>
    <circle cx="{eye_x:.1f}" cy="{eye_y:.1f}" r="{eye_r:.1f}" fill="#B03A2E"/>
  </g>
  <rect x="6" y="6" width="988" height="988" rx="156" fill="none" stroke="#8B2A1F" stroke-opacity="0.59" stroke-width="12"/>
</svg>
"""


def main(argv: list[str] | None = None) -> None:
    """生成 ICO / PNG / SVG；``--preview`` 只出三套变体对比图，供人工/视觉模型选型。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--preview" in args:
        for variant in ("merged", "seal", "float"):
            render(256, variant).save(HERE / f"preview-{variant}.png")
            print("preview:", f"preview-{variant}.png")
        return

    variant = "merged"
    if "--variant" in args:
        variant = args[args.index("--variant") + 1]

    sizes = [256, 128, 64, 48, 32, 24, 16]
    base = render(256, variant)
    base.save(HERE / "fishcloud.ico", sizes=[(s, s) for s in sizes])
    base.save(HERE / "fishcloud-256.png")
    render(128, variant).save(HERE / "fishcloud-128.png")
    render(64, variant).save(HERE / "fishcloud-64.png")
    render(32, variant).save(HERE / "fishcloud-32.png")
    (HERE / "fishcloud-icon.svg").write_text(svg_source(), encoding="utf-8")
    print(f"variant={variant} written:", ", ".join(p.name for p in sorted(HERE.glob("fishcloud*"))))


if __name__ == "__main__":
    main()
