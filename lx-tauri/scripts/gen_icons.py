#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从一张母版图片生成 Tauri 所需的全部图标。

用法：
    python scripts/gen_icons.py                    # 用默认母版 scripts/_source_icon.webp
    python scripts/gen_icons.py path/to/icon.png   # 指定其它母版

母版要求：正方形、建议 ≥1024px、背景已自带圆角（透明角）。
脚本只做等比缩放与格式封装，不改变原图造型，保证设计稿所见即所得。

产物：
    public/icon.svg（favicon 用的占位；母版为位图时自动内嵌 PNG）
    src-tauri/icons/*.png / *.ico / *.icns
"""
import base64
import io
import os
import struct
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PUBLIC = os.path.join(ROOT, "public")
ICONS = os.path.join(ROOT, "src-tauri", "icons")
DEFAULT_MASTER = os.path.join(HERE, "_source_icon.webp")

# Tauri 打包会读取这些文件名，必须齐全
PNG_SIZES = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
    "Square30x30Logo.png": 30,
    "Square44x44Logo.png": 44,
    "Square71x71Logo.png": 71,
    "Square89x89Logo.png": 89,
    "Square107x107Logo.png": 107,
    "Square142x142Logo.png": 142,
    "Square150x150Logo.png": 150,
    "Square284x284Logo.png": 284,
    "Square310x310Logo.png": 310,
    "StoreLogo.png": 50,
}

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def load_master(path):
    im = Image.open(path)
    im = im.convert("RGBA")
    w, h = im.size
    if w != h:
        # 非正方形：居中裁成正方形，避免缩放变形
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
    return im


def resize(im, size):
    """缩放。图标缩小时用 LANCZOS 保边，放大不涉及（母版已足够大）。"""
    return im.resize((size, size), Image.LANCZOS)


def write_ico(master, path):
    """ICO 多尺寸封装。Pillow 的 ICO 保存会按 sizes 参数逐个缩放。"""
    master.save(path, format="ICO", sizes=ICO_SIZES)


def write_icns(master, path):
    """手写 .icns 容器（Pillow 在 Windows 上不能直接写 ICNS）。"""
    specs = [
        (b"icp4", 16), (b"icp5", 32), (b"icp6", 64),
        (b"ic07", 128), (b"ic08", 256), (b"ic09", 512),
        (b"ic10", 1024), (b"ic11", 32), (b"ic12", 64),
        (b"ic13", 256), (b"ic14", 512),
    ]
    chunks = b""
    for code, size in specs:
        buf = io.BytesIO()
        resize(master, size).save(buf, format="PNG")
        data = buf.getvalue()
        chunks += code + struct.pack(">I", len(data) + 8) + data
    with open(path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)


def write_favicon(master, path):
    """favicon：位图母版直接内嵌为 data URI 的 SVG（浏览器同样能显示）。"""
    buf = io.BytesIO()
    resize(master, 256).save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'viewBox="0 0 256 256" role="img" aria-label="落雪下载">\n'
        f'  <image width="256" height="256" xlink:href="data:image/png;base64,{b64}"/>\n'
        "</svg>\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MASTER
    if not os.path.isfile(src):
        print(f"母版不存在: {src}")
        return 1
    os.makedirs(ICONS, exist_ok=True)
    os.makedirs(PUBLIC, exist_ok=True)

    master = load_master(src)
    print(f"母版: {src}  尺寸: {master.size}")

    for name, size in PNG_SIZES.items():
        resize(master, size).save(os.path.join(ICONS, name), optimize=True)
    write_ico(master, os.path.join(ICONS, "icon.ico"))
    write_icns(master, os.path.join(ICONS, "icon.icns"))
    write_favicon(master, os.path.join(PUBLIC, "icon.svg"))

    print(f"已生成 {len(PNG_SIZES)} 个 PNG + icon.ico + icon.icns -> {ICONS}")
    print(f"favicon -> {os.path.join(PUBLIC, 'icon.svg')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
