#!/usr/bin/env python3
"""Draw WGLink's toolbar icons.

Fusion wants one folder per command holding 16x16, 32x32 and 64x64 PNGs. Each
glyph is drawn once at 512 px and downsampled, because Fusion's 16 px slot is
where an icon either reads or turns to mush -- shapes are kept few, thick, and
far apart for that size rather than for the 64 px preview.

Every command shows the same waveguide mouth so the set reads as one family;
the accent mark on top is what distinguishes them.
"""

from __future__ import annotations

from pathlib import Path
import sys

from PIL import Image, ImageDraw


REPO_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = REPO_ROOT / "fusion-addins" / "WGLink" / "resources"
SIZES = (16, 32, 64)
SUPERSAMPLE = 512
SCALE = SUPERSAMPLE / 64  # glyphs below are authored on a 64-unit grid

INK = (60, 64, 72, 255)
ACCENT = (198, 106, 42, 255)     # WG ember
POSITIVE = (58, 132, 94, 255)
NEGATIVE = (166, 58, 58, 255)


def _s(value: float) -> float:
    return value * SCALE


def _w(value: float) -> int:
    """Stroke widths reach PIL as pixels, so they must be whole numbers."""

    return max(1, round(value * SCALE))


def _box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
    return (_s(x0), _s(y0), _s(x1), _s(y1))


def _mouth(draw: ImageDraw.ImageDraw, colour=INK) -> None:
    """The waveguide mouth: two nested rounded rectangles, seen head on."""

    draw.rounded_rectangle(_box(6, 20, 58, 56), radius=_s(6), outline=colour, width=_w(3.4))
    draw.rounded_rectangle(_box(19, 30, 45, 46), radius=_s(4), outline=colour, width=_w(3.0))


def _arrow_down(draw: ImageDraw.ImageDraw, colour) -> None:
    draw.line([(_s(32), _s(2)), (_s(32), _s(15))], fill=colour, width=_w(4.6))
    draw.polygon(
        [(_s(23), _s(11)), (_s(41), _s(11)), (_s(32), _s(23))], fill=colour
    )


def _arrow_up(draw: ImageDraw.ImageDraw, colour) -> None:
    draw.line([(_s(32), _s(10)), (_s(32), _s(23))], fill=colour, width=_w(4.6))
    draw.polygon([(_s(23), _s(14)), (_s(41), _s(14)), (_s(32), _s(2))], fill=colour)


def draw_insert(draw: ImageDraw.ImageDraw) -> None:
    _mouth(draw)
    _arrow_down(draw, ACCENT)


def draw_update(draw: ImageDraw.ImageDraw) -> None:
    _mouth(draw)
    # An open circular arrow: arc plus a head, the usual "refresh in place".
    draw.arc(_box(18, 0, 46, 24), start=150, end=30, fill=ACCENT, width=_w(4.4))
    draw.polygon([(_s(44), _s(2)), (_s(50), _s(13)), (_s(38), _s(12))], fill=ACCENT)


def draw_audit(draw: ImageDraw.ImageDraw) -> None:
    _mouth(draw)
    draw.ellipse(_box(20, 0, 42, 22), outline=ACCENT, width=_w(4.2))
    draw.line([(_s(40), _s(19)), (_s(50), _s(28))], fill=ACCENT, width=_w(4.6))


def draw_send(draw: ImageDraw.ImageDraw) -> None:
    _mouth(draw)
    _arrow_up(draw, POSITIVE)


def draw_solve(draw: ImageDraw.ImageDraw) -> None:
    # Solve in WG is a send that also starts a run: the send arrow, with the
    # play mark that names the difference.
    _mouth(draw)
    _arrow_up(draw, POSITIVE)
    draw.polygon([(_s(44), _s(2)), (_s(44), _s(22)), (_s(60), _s(12))], fill=ACCENT)


def draw_relink(draw: ImageDraw.ImageDraw) -> None:
    _mouth(draw)
    # Two interlocking links, drawn as capsules that overlap at the centre.
    draw.rounded_rectangle(_box(16, 4, 36, 20), radius=_s(8), outline=ACCENT, width=_w(4.0))
    draw.rounded_rectangle(_box(30, 4, 50, 20), radius=_s(8), outline=ACCENT, width=_w(4.0))


def draw_detach(draw: ImageDraw.ImageDraw) -> None:
    _mouth(draw)
    # The same two links, pulled apart, with the break shown as a gap.
    draw.rounded_rectangle(_box(11, 4, 29, 20), radius=_s(8), outline=NEGATIVE, width=_w(4.0))
    draw.rounded_rectangle(_box(37, 4, 55, 20), radius=_s(8), outline=NEGATIVE, width=_w(4.0))


def draw_manage(draw: ImageDraw.ImageDraw) -> None:
    _mouth(draw)
    draw.line([(_s(18), _s(4)), (_s(46), _s(4))], fill=INK, width=_w(3.2))
    draw.ellipse(_box(22, 0, 30, 8), fill=ACCENT)
    draw.line([(_s(18), _s(12)), (_s(46), _s(12))], fill=INK, width=_w(3.2))
    draw.ellipse(_box(34, 8, 42, 16), fill=ACCENT)
    draw.line([(_s(18), _s(20)), (_s(46), _s(20))], fill=INK, width=_w(3.2))
    draw.ellipse(_box(26, 16, 34, 24), fill=ACCENT)


# --- compact variants -------------------------------------------------------
# At 16 px the nested mouth collapses into a grey blob and the accent mark that
# actually names the command disappears. The compact glyph drops the mouth to a
# single bar and gives the mark the whole canvas instead.


def _bar(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle(_box(8, 46, 56, 58), radius=_s(4), fill=INK)


def compact_insert(draw: ImageDraw.ImageDraw) -> None:
    _bar(draw)
    draw.line([(_s(32), _s(4)), (_s(32), _s(24))], fill=ACCENT, width=_w(7))
    draw.polygon([(_s(17), _s(20)), (_s(47), _s(20)), (_s(32), _s(40))], fill=ACCENT)


def compact_update(draw: ImageDraw.ImageDraw) -> None:
    _bar(draw)
    draw.arc(_box(10, 2, 54, 42), start=140, end=40, fill=ACCENT, width=_w(7.5))
    draw.polygon([(_s(48), _s(0)), (_s(60), _s(20)), (_s(36), _s(19))], fill=ACCENT)


def compact_audit(draw: ImageDraw.ImageDraw) -> None:
    _bar(draw)
    draw.ellipse(_box(12, 2, 44, 34), outline=ACCENT, width=_w(7))
    draw.line([(_s(40), _s(30)), (_s(56), _s(44))], fill=ACCENT, width=_w(8))


def compact_send(draw: ImageDraw.ImageDraw) -> None:
    _bar(draw)
    draw.line([(_s(32), _s(20)), (_s(32), _s(40))], fill=POSITIVE, width=_w(7))
    draw.polygon([(_s(17), _s(24)), (_s(47), _s(24)), (_s(32), _s(4))], fill=POSITIVE)


def compact_solve(draw: ImageDraw.ImageDraw) -> None:
    _bar(draw)
    draw.polygon([(_s(6), _s(4)), (_s(6), _s(40)), (_s(34), _s(22))], fill=POSITIVE)
    draw.polygon([(_s(36), _s(4)), (_s(36), _s(40)), (_s(62), _s(22))], fill=ACCENT)


def compact_relink(draw: ImageDraw.ImageDraw) -> None:
    _bar(draw)
    draw.rounded_rectangle(_box(4, 10, 36, 38), radius=_s(14), outline=ACCENT, width=_w(7))
    draw.rounded_rectangle(_box(28, 10, 60, 38), radius=_s(14), outline=ACCENT, width=_w(7))


def compact_detach(draw: ImageDraw.ImageDraw) -> None:
    _bar(draw)
    draw.arc(_box(0, 10, 30, 38), start=90, end=270, fill=NEGATIVE, width=_w(7.5))
    draw.arc(_box(34, 10, 64, 38), start=270, end=90, fill=NEGATIVE, width=_w(7.5))


def compact_manage(draw: ImageDraw.ImageDraw) -> None:
    _bar(draw)
    draw.line([(_s(6), _s(8)), (_s(58), _s(8))], fill=INK, width=_w(6))
    draw.ellipse(_box(14, 2, 26, 14), fill=ACCENT)
    draw.line([(_s(6), _s(23)), (_s(58), _s(23))], fill=INK, width=_w(6))
    draw.ellipse(_box(38, 17, 50, 29), fill=ACCENT)
    draw.line([(_s(6), _s(38)), (_s(58), _s(38))], fill=INK, width=_w(6))
    draw.ellipse(_box(24, 32, 36, 44), fill=ACCENT)


GLYPHS = {
    "solve": (draw_solve, compact_solve),
    "insert": (draw_insert, compact_insert),
    "update": (draw_update, compact_update),
    "audit": (draw_audit, compact_audit),
    "send": (draw_send, compact_send),
    "relink": (draw_relink, compact_relink),
    "detach": (draw_detach, compact_detach),
    "manage": (draw_manage, compact_manage),
}


def _rendered(painter) -> Image.Image:
    canvas = Image.new("RGBA", (SUPERSAMPLE, SUPERSAMPLE), (0, 0, 0, 0))
    painter(ImageDraw.Draw(canvas))
    return canvas


def render(name: str, painters) -> None:
    detailed, compact = painters
    art = {size: _rendered(compact if size <= 16 else detailed) for size in SIZES}
    folder = RESOURCES / name
    folder.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        art[size].resize((size, size), Image.LANCZOS).save(folder / f"{size}x{size}.png")


def main() -> int:
    for name, painters in GLYPHS.items():
        render(name, painters)
        print(f"wrote {RESOURCES.relative_to(REPO_ROOT) / name} ({', '.join(f'{s}x{s}' for s in SIZES)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
