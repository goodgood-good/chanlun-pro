# -*- coding: utf-8 -*-
from __future__ import annotations

import html
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


COVER_WIDTH = 1600
COVER_HEIGHT = 2400


def font_path(*names: str) -> Path:
    for name in names:
        candidate = Path(r"C:\Windows\Fonts") / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No suitable Chinese font found in C:\\Windows\\Fonts")


def load_font(size: int, bold: bool = False, serif: bool = False) -> ImageFont.FreeTypeFont:
    if serif:
        candidates = ("NotoSerifSC-VF.ttf", "simsun.ttc", "simsunb.ttf")
    elif bold:
        candidates = ("NotoSansSC-VF.ttf", "msyhbd.ttc", "simhei.ttf")
    else:
        candidates = ("NotoSansSC-VF.ttf", "msyh.ttc", "Deng.ttf")
    return ImageFont.truetype(str(font_path(*candidates)), size)


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start_size: int,
    *,
    bold: bool = False,
    serif: bool = False,
    min_size: int = 36,
) -> ImageFont.FreeTypeFont:
    size = start_size
    while size >= min_size:
        font = load_font(size, bold=bold, serif=serif)
        width, _ = text_size(draw, text, font)
        if width <= max_width:
            return font
        size -= 4
    return load_font(min_size, bold=bold, serif=serif)


def center_text(
    draw: ImageDraw.ImageDraw,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    *,
    width: int = COVER_WIDTH,
) -> int:
    text_width, text_height = text_size(draw, text, font)
    draw.text(((width - text_width) / 2, y), text, font=font, fill=fill)
    return y + text_height


def draw_market_line(draw: ImageDraw.ImageDraw) -> None:
    points = [
        (230, 1540),
        (360, 1460),
        (500, 1515),
        (650, 1320),
        (790, 1375),
        (940, 1190),
        (1110, 1260),
        (1280, 1040),
        (1390, 1125),
    ]
    draw.line(points, fill=(40, 93, 79), width=8, joint="curve")
    for x, y in points:
        draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=(40, 93, 79))

    candles = [
        (330, 1380, 1510, True),
        (470, 1450, 1560, False),
        (620, 1265, 1395, True),
        (775, 1320, 1435, False),
        (930, 1120, 1245, True),
        (1095, 1190, 1320, False),
        (1260, 980, 1100, True),
    ]
    for x, y1, y2, up in candles:
        color = (40, 93, 79) if up else (154, 78, 64)
        draw.line((x, y1 - 55, x, y2 + 55), fill=color, width=5)
        draw.rounded_rectangle((x - 22, y1, x + 22, y2), radius=4, outline=color, width=5)


def create_clean_cover(
    output_path: Path,
    *,
    title: str,
    subtitle: str,
    range_label: str,
    volume_label: str | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), (247, 244, 238))
    draw = ImageDraw.Draw(image)

    # Subtle paper texture and a quiet technical grid.
    for y in range(0, COVER_HEIGHT, 24):
        shade = 235 if y % 48 == 0 else 240
        draw.line((120, y, COVER_WIDTH - 120, y), fill=(shade, shade, shade), width=1)
    for x in range(120, COVER_WIDTH - 119, 40):
        draw.line((x, 0, x, COVER_HEIGHT), fill=(241, 240, 236), width=1)

    draw.rectangle((0, 0, 38, COVER_HEIGHT), fill=(35, 82, 71))
    draw.rectangle((38, 0, 46, COVER_HEIGHT), fill=(187, 148, 80))
    draw.rounded_rectangle((170, 170, COVER_WIDTH - 170, COVER_HEIGHT - 170), radius=0, outline=(218, 211, 199), width=3)

    title_font = fit_font(draw, title, 1040, 210, bold=True, serif=True, min_size=130)
    subtitle_font = fit_font(draw, subtitle, 1040, 54, bold=False, serif=False, min_size=38)
    volume_font = fit_font(draw, volume_label or "全本", 700, 78, bold=True, serif=False, min_size=52)
    range_font = fit_font(draw, range_label, 920, 58, bold=False, serif=False, min_size=38)
    author_font = fit_font(draw, "缠中说禅", 720, 48, bold=False, serif=True, min_size=36)

    y = 520
    if volume_label:
        y = center_text(draw, y, volume_label, volume_font, (91, 81, 64)) + 86
    y = center_text(draw, y, title, title_font, (24, 49, 43)) + 64
    y = center_text(draw, y, subtitle, subtitle_font, (74, 83, 78)) + 46
    center_text(draw, y, range_label, range_font, (116, 93, 55))

    draw_market_line(draw)

    draw.line((470, 1802, COVER_WIDTH - 470, 1802), fill=(187, 148, 80), width=3)
    center_text(draw, 1870, "缠中说禅", author_font, (74, 68, 58))

    note_font = load_font(34)
    note = "整理为电子书阅读版"
    note_width, note_height = text_size(draw, note, note_font)
    draw.text(((COVER_WIDTH - note_width) / 2, 2045), note, font=note_font, fill=(118, 113, 103))

    image.save(output_path, format="JPEG", quality=95, subsampling=0, optimize=True)


def cover_xhtml(title: str, image_href: str = "../Images/cover.jpg") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../Styles/style.css"/>
</head>
<body class="cover-body">
  <div class="cover-page">
    <img class="cover-image" src="{html.escape(image_href)}" alt="{html.escape(title)}"/>
  </div>
</body>
</html>
"""
