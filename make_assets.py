"""生成项目 ICON（512）。

⚠️ 封面已换：正式封面 assets/cover-16x9.png 由 make_cover_v3.py 生成
   （登录页风格：夜色海面 + 三张冰牌）。本脚本的封面已停用，
   只输出 cover-16x9-旧版画舫.png，避免把线上封面覆盖掉。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ASSETS = Path(__file__).parent / "assets"
ASSETS.mkdir(exist_ok=True)

F = "C:/Windows/Fonts/msyh.ttc"
FB = "C:/Windows/Fonts/msyhbd.ttc"

SKY_TOP = (233, 243, 251)
SKY_BOT = (247, 251, 253)
BRAND = (62, 124, 177)
BRAND_DEEP = (44, 95, 141)
BRAND_LIGHT = (107, 163, 208)
ACCENT = (232, 163, 61)
ACCENT_SOFT = (253, 243, 227)
INK = (31, 45, 61)
SUB = (107, 130, 153)
WHITE = (255, 255, 255)
ICE = (226, 238, 247)


def vgrad(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (1, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.point((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img.resize((w, h))


def boat(d, cx, base_y, scale, hull_col=WHITE, inner=None, deck=None):
    """破冰船（正面视角）：白船头 + 明显更窄的船体 + 甲板线

    关键：船体必须比船头明显窄，且颜色不同，否则会糊成一个色块。
    """
    w = 1.0 * scale
    hc = hull_col if hull_col != WHITE else (107, 163, 208)
    # 船体（先画，在船头下面）
    d.polygon([(cx - 0.78 * w, base_y), (cx + 0.78 * w, base_y),
               (cx + 0.40 * w, base_y + 0.40 * w), (cx - 0.40 * w, base_y + 0.40 * w)],
              fill=hc)
    # 船头（V 形，白）
    d.polygon([(cx, base_y - 1.0 * w), (cx + 0.78 * w, base_y), (cx - 0.78 * w, base_y)],
              fill=hull_col)
    # 甲板线（琥珀），把船头与船体分开
    dl = deck or ACCENT
    d.line([(cx - 0.78 * w, base_y), (cx + 0.78 * w, base_y)], fill=dl,
           width=max(4, int(0.07 * w)))
    if inner:
        d.polygon([(cx, base_y - 0.52 * w), (cx + 0.36 * w, base_y - 0.06 * w),
                   (cx, base_y + 0.14 * w), (cx - 0.36 * w, base_y - 0.06 * w)], fill=inner)


def hull_bottom(base_y, scale):
    """船底 y 坐标（裂纹从这里散开）"""
    return base_y + 0.40 * scale


def cracks(d, cx, y, span, col=ACCENT, wid=9):
    """冰裂纹"""
    d.line([(cx - span, y), (cx - span * 0.42, y), (cx - span * 0.2, y - 10),
            (cx, y + 6), (cx + span * 0.2, y - 10), (cx + span * 0.42, y), (cx + span, y)],
           fill=col, width=wid, joint="curve")
    d.line([(cx, y + 6), (cx, y + 34)], fill=col, width=int(wid * 0.8))
    d.line([(cx - span * 0.2, y - 10), (cx - span * 0.34, y - 30)], fill=col, width=int(wid * 0.6))
    d.line([(cx + span * 0.2, y - 10), (cx + span * 0.34, y - 30)], fill=col, width=int(wid * 0.6))


def wave(d, x, y, w, col=WHITE, wid=6, alpha_hint=None):
    d.line([(x, y), (x + w * 0.25, y - 7), (x + w * 0.5, y), (x + w * 0.75, y - 7), (x + w, y)],
           fill=col, width=wid, joint="curve")


# ============================================================
# ICON · 512x512（小尺寸优先：简洁、对比强）
# ============================================================
S = 512
icon = vgrad((S, S), (96, 156, 204), (44, 95, 141))
d = ImageDraw.Draw(icon)

# 太阳（右上）
d.ellipse([372, 54, 448, 130], fill=(246, 199, 122))
d.ellipse([387, 69, 433, 115], fill=ACCENT)

# 水面带（底部，浅一档，作为地平线）
d.polygon([(0, 374), (128, 356), (256, 374), (384, 356), (S, 374), (S, S), (0, S)],
          fill=(56, 112, 156))

# 船（居中，坐在水面上）
boat(d, 256, 318, 134, hull_col=WHITE)
# 冰裂纹（从船底散开）
cracks(d, 256, hull_bottom(318, 134) + 24, 132, col=ACCENT, wid=15)
# 水波
wave(d, 116, 452, 116, col=WHITE, wid=9)
wave(d, 288, 452, 116, col=WHITE, wid=9)

# 圆角裁切
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=112, fill=255)
icon_out = Image.new("RGB", (S, S), WHITE)
icon_out.paste(icon, (0, 0), mask)
icon_out.save(str(ASSETS / "icon-512.png"))
print("✅ ICON   512x512  → assets/icon-512.png")


# ============================================================
# 封面 · 1600x900（16:9）—— 左文右图，元素分区不重叠
# ============================================================
W, H = 1600, 900
HORIZON = 684                     # 地平线（冰面起点）

cov = vgrad((W, H), SKY_TOP, SKY_BOT)

# 右上光晕（只在右半，避免影响左侧文字对比度）
glow = Image.new("RGB", (W, H), SKY_BOT)
gd = ImageDraw.Draw(glow)
gd.ellipse([860, -320, 1620, 440], fill=(255, 250, 238))
glow = glow.filter(ImageFilter.GaussianBlur(110))
cov = Image.blend(cov, glow, 0.5)
d = ImageDraw.Draw(cov)

# 太阳
d.ellipse([1372, 88, 1472, 188], fill=(246, 199, 122))
d.ellipse([1388, 104, 1456, 172], fill=ACCENT)

# ---- 冰面（两层，制造纵深感）----
d.polygon([(0, HORIZON), (240, HORIZON - 26), (470, HORIZON - 6), (700, HORIZON - 30),
           (930, HORIZON - 4), (1170, HORIZON - 28), (1400, HORIZON - 8), (W, HORIZON - 22),
           (W, H), (0, H)],
          fill=(228, 240, 249))
d.polygon([(0, HORIZON + 62), (200, HORIZON + 30), (430, HORIZON + 68), (690, HORIZON + 26),
           (940, HORIZON + 70), (1200, HORIZON + 28), (W, HORIZON + 66), (W, H), (0, H)],
          fill=(214, 233, 246))

# ---- 船（右侧，坐在冰面上）----
BX, BY, BS = 1210, HORIZON + 6, 118
boat(d, BX, BY, BS, hull_col=WHITE, inner=BRAND_LIGHT)
# 冰裂纹（从船底向两侧，同一水平线）
cracks(d, BX, hull_bottom(BY, BS) + 20, 300, col=ACCENT, wid=11)
# 水波
for (x, y, w) in [(120, 826, 240), (430, 862, 170), (980, 844, 210), (1320, 878, 190)]:
    wave(d, x, y, w, col=(255, 255, 255), wid=8)

# ---- 右侧：两个分身的气泡（斜向排布，与船不重叠）----
def bubble(d, x, y, w, h, fill, tail_left=True, lines=2, line_col=None):
    d.rounded_rectangle([x, y, x + w, y + h], radius=24, fill=fill)
    if tail_left:
        d.polygon([(x + 32, y + h - 2), (x + 28, y + h + 24), (x + 66, y + h - 2)], fill=fill)
    else:
        d.polygon([(x + w - 66, y + h - 2), (x + w - 28, y + h + 24), (x + w - 32, y + h - 2)], fill=fill)
    lc = line_col or (255, 255, 255)
    yy = y + 26
    for i in range(lines):
        lw = int(w * (0.60 if i == lines - 1 else 0.72))
        d.rounded_rectangle([x + 26, yy, x + 26 + lw, yy + 12], radius=6, fill=lc)
        yy += 28

bubble(d, 922, 292, 338, 112, fill=WHITE, tail_left=True, lines=2, line_col=(202, 218, 231))
bubble(d, 1114, 424, 330, 102, fill=BRAND, tail_left=False, lines=2, line_col=(230, 240, 249))

f_tag = ImageFont.truetype(F, 22)
d.text((946, 254), "TA 的分身", font=f_tag, fill=SUB)
d.text((1136, 386), "我的分身", font=f_tag, fill=BRAND_DEEP)

# ---- 左侧：文字区 ----
f_sub = ImageFont.truetype(F, 26)
f_h1 = ImageFont.truetype(FB, 110)
f_tagline = ImageFont.truetype(F, 35)
f_small = ImageFont.truetype(F, 26)

d.text((116, 262), "知乎黑客松 2026 · 灵魂匹配局", font=f_sub, fill=(150, 168, 184))

d.text((112, 316), "社恐", font=f_h1, fill=INK)
w1 = d.textlength("社恐", font=f_h1)
d.text((112 + w1, 316), "破冰船", font=f_h1, fill=BRAND)

d.text((118, 470), "超想认识你，那先跟你的", font=f_tagline, fill=SUB)
d.text((118, 518), "分身 Agent 聊聊吧", font=f_tagline, fill=SUB)

# 分隔线
d.line([(120, 600), (520, 600)], fill=(212, 228, 240), width=3)

d.text((118, 626), "分身先替你去见 TA，", font=f_small, fill=(140, 160, 178))
d.text((118, 664), "你再看要不要真的开口。", font=f_small, fill=(140, 160, 178))

cov.save(str(ASSETS / "cover-16x9-旧版画舫.png"))
print("⚠️  旧版封面 1600x900 → assets/cover-16x9-旧版画舫.png（正式封面请跑 make_cover_v3.py）")
