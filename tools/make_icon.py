"""生成插件图标 tuoz1.ico（方案 A：海克斯金六边形 + 警星）

只在改图标时运行一次（需要 Pillow），生成的 tuoz1.ico 直接提交进仓库；
打包时 PyInstaller 用 --icon tuoz1.ico 嵌进 exe，再用 --add-data 带上给窗口标题栏用。
    python tools/make_icon.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

GOLD, NAVY, STAR = "#C8AA6E", "#0A1428", "#F0E6D2"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
SS = 8                      # 先放大 8 倍画再缩小，边缘平滑


def hexagon(cx, cy, r):
    """尖朝上的正六边形"""
    return [(cx + r * math.cos(math.radians(-90 + 60 * k)), cy + r * math.sin(math.radians(-90 + 60 * k))) for k in range(6)]


def star(cx, cy, ro, ri):
    return [(cx + (ro if k % 2 == 0 else ri) * math.cos(math.radians(-90 + 36 * k)),
             cy + (ro if k % 2 == 0 else ri) * math.sin(math.radians(-90 + 36 * k))) for k in range(10)]


def render(size: int) -> Image.Image:
    n = size * SS
    u = n / 64                                   # 设计稿是 64×64
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    frame = max(4.0, 1.3 * 64 / size)            # 小尺寸把金边加粗，16px 下也至少一像素多
    d.polygon([(x * u, y * u) for x, y in hexagon(32, 32, 28)], fill=GOLD)
    d.polygon([(x * u, y * u) for x, y in hexagon(32, 32, 28 - frame)], fill=NAVY)
    ro = 14 if size >= 32 else 15
    d.polygon([(x * u, y * u) for x, y in star(32, 33, ro, ro * 0.4)], fill=STAR)
    return img.resize((size, size), Image.LANCZOS)


def main():
    out = Path(__file__).resolve().parent.parent / "tuoz1.ico"
    imgs = [render(s) for s in SIZES]
    imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES], append_images=imgs[:-1])
    print(f"已生成 {out}（{', '.join(map(str, SIZES))}）")


if __name__ == "__main__":
    main()
