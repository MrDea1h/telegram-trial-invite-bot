"""The prize wheel as a GIF: it spins, stops with the pointer on the winning sector, and the
winning sector is then lit while the rest of the wheel goes dim.

The picture only shows a result that was already drawn and stored, so it decides nothing.
Pillow is imported inside the functions: without it the bot still works and answers in text.

Branding is optional and lives outside the code: `logo` is a path to a square picture shown
in the hub (its inscribed circle is used), `brand` is a short word for the plaque below.
"""

import io
import math
import os
from pathlib import Path

WIDTH, HEIGHT = 400, 440
CENTER = (200, 204)
RADIUS = 164  # of the sectors; the ink frame goes around it
HUB = 44
SCALE = 2  # still parts are drawn larger and scaled down for clean edges

FRAMES = 44
FRAME_MS = 50
BLINK_MS = (260, 200, 180, 200, 180)  # stopped wheel, then dim / lit / dim / lit
HOLD_MS = 8000  # the last, dimmed frame stays, so the result can be read before the loop restarts
TURNS = 4
MIN_SECTORS = 6  # a short prize list is repeated around the wheel
MAX_LABEL = 40

INK = (13, 11, 16)
LEMON = (255, 248, 81)
VIOLET = (164, 0, 255)
CORAL = (255, 133, 98)
NIGHT = (36, 16, 79)
MAGENTA = (255, 61, 242)
WHITE = (255, 255, 255)
# (sector colour, text colour)
SECTORS = [
    (VIOLET, WHITE),
    (WHITE, INK),
    (CORAL, INK),
    (NIGHT, LEMON),
    ((235, 223, 250), INK),
    ((105, 34, 191), WHITE),
]

FONTS = (
    os.environ.get("WHEEL_FONT", ""),
    str(Path(__file__).parent / "assets" / "Geologica-ExtraBold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def sectors(count: int) -> int:
    """How many sectors a wheel with `count` prizes has."""
    repeat = 1
    while count * repeat < MIN_SECTORS:
        repeat += 1
    return count * repeat


def stop_angle(count: int, winner: int, offset: float = 0.0) -> float:
    """Clockwise turn, in degrees, that leaves sector `winner` under the pointer at the top.

    `offset` (-0.5..0.5 of a sector) moves the stop point away from the sector's middle.
    """
    step = 360 / sectors(count)
    middle = (winner + 0.5 + offset) * step  # clockwise from 3 o'clock, before any turn
    return (270 - middle) % 360 + 360 * TURNS


def _font(size: int):
    from PIL import ImageFont

    for path in FONTS:
        if path:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _label(draw, text: str, room: int, size: int, smallest: int, two_lines: bool):
    """The font and text that fit `room` pixels: one line if it fits without getting small,
    else two lines, else one line cut short."""
    text = " ".join(text.split())
    if len(text) > MAX_LABEL:
        text = text[: MAX_LABEL - 1].rstrip() + "…"

    def fits(lines, at):
        font = _font(at)
        return font if all(draw.textlength(line, font=font) <= room for line in lines) else None

    for at in range(size, int(size * 0.8) - 1, -1):
        if font := fits([text], at):
            return font, text
    spaces = [index for index, char in enumerate(text) if char == " "]
    if two_lines and spaces:
        cut = min(spaces, key=lambda index: abs(index - len(text) / 2))
        lines = [text[:cut], text[cut + 1 :]]
        for at in range(int(size * 0.9), smallest - 1, -1):
            if font := fits(lines, at):
                return font, "\n".join(lines)
    for at in range(int(size * 0.8), smallest - 1, -1):
        if font := fits([text], at):
            return font, text
    font = _font(smallest)
    while draw.textlength(text, font=font) > room and len(text) > 2:
        text = text[:-2].rstrip() + "…"
    return font, text


def _sparkle(draw, x: float, y: float, size: float, fill, width: int) -> None:
    """A four-point star with curved-in sides."""
    points = []
    for step in range(72):
        angle = math.radians(step * 5)
        points.append(
            (
                x + size * math.copysign(abs(math.cos(angle)) ** 3, math.cos(angle)),
                y + size * math.copysign(abs(math.sin(angle)) ** 3, math.sin(angle)),
            )
        )
    draw.polygon(points, fill=fill, outline=INK, width=width)


def _dot(draw, x: float, y: float, radius: float, fill, width: int) -> None:
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius), fill=fill, outline=INK, width=width
    )


def _backdrop():
    """What never moves and lies under the wheel: the field, the sparkles, the wheel's shadow."""
    from PIL import Image, ImageDraw

    s = SCALE
    image = Image.new("RGB", (WIDTH * s, HEIGHT * s), LEMON)
    draw = ImageDraw.Draw(image)
    cx, cy = CENTER[0] * s, CENTER[1] * s
    # An orbit behind the wheel, as on the school's pictures.
    orbit = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(orbit).ellipse(
        (cx - 250 * s, cy - 120 * s, cx + 250 * s, cy + 120 * s),
        outline=(181, 173, 58, 255),
        width=3 * s,
    )
    orbit = orbit.rotate(18, resample=Image.Resampling.BICUBIC, center=(cx, cy))
    image.paste(orbit, (0, 0), orbit)
    _sparkle(draw, 36 * s, 44 * s, 26 * s, MAGENTA, 3 * s)
    _sparkle(draw, 368 * s, 396 * s, 18 * s, WHITE, 3 * s)
    _dot(draw, 366 * s, 46 * s, 13 * s, CORAL, 3 * s)
    _dot(draw, 30 * s, 388 * s, 10 * s, VIOLET, 3 * s)
    outer = (RADIUS + 9) * s
    shadow = 7 * s  # a hard shadow: the same shape moved down and right
    draw.ellipse(
        (cx - outer + shadow, cy - outer + shadow, cx + outer + shadow, cy + outer + shadow),
        fill=INK,
    )
    return image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def _wheel(titles: list[str]):
    """The turning part, unturned: a square picture with the wheel's centre in its middle."""
    from PIL import Image, ImageDraw

    s = SCALE
    outer = (RADIUS + 9) * s
    radius = RADIUS * s
    side = (outer + 2 * s) * 2
    center = side // 2
    total = sectors(len(titles))
    step = 360 / total
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((center - outer, center - outer, center + outer, center + outer), fill=INK)
    box = (center - radius, center - radius, center + radius, center + radius)
    inner, edge = (HUB + 12) * s, radius - 12 * s
    biggest = (17 if total <= 8 else 14 if total <= 14 else 11) * s
    for index in range(total):
        color, text_color = SECTORS[index % len(SECTORS)]
        if total % len(SECTORS) == 1 and index == total - 1:
            color, text_color = SECTORS[2]  # the last sector must not repeat the first one's
        start = index * step
        draw.pieslice(box, start, start + step, fill=color, outline=INK, width=2 * s)
        # Text is drawn along the radius at 3 o'clock, then the layer is turned into place.
        layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        pen = ImageDraw.Draw(layer)
        font, text = _label(
            pen, titles[index % len(titles)], edge - inner, biggest, 9 * s, total <= 16
        )
        pen.multiline_text(
            (center + edge, center), text, font=font, anchor="rm", align="right",
            spacing=2 * s, fill=text_color,
        )  # fmt: skip
        layer = layer.rotate(-(index + 0.5) * step, resample=Image.Resampling.BICUBIC)
        image.alpha_composite(layer)
    # Studs on the frame make the turning visible even on a wheel of similar sectors.
    for index in range(total):
        angle = math.radians(index * step)
        x, y = (
            center + (outer - 5 * s) * math.cos(angle),
            center + (outer - 5 * s) * math.sin(angle),
        )
        draw.ellipse((x - 3 * s, y - 3 * s, x + 3 * s, y + 3 * s), fill=LEMON)
    return image.resize((side // s, side // s), Image.Resampling.LANCZOS)


def _overlay(logo: str | None, brand: str):
    """What never moves and lies over the wheel: the hub, the pointer and the brand plaque."""
    from PIL import Image, ImageDraw

    s = SCALE
    image = Image.new("RGBA", (WIDTH * s, HEIGHT * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, cy = CENTER[0] * s, CENTER[1] * s

    hub = HUB * s
    draw.ellipse((cx - hub + 4 * s, cy - hub + 4 * s, cx + hub + 4 * s, cy + hub + 4 * s), fill=INK)
    draw.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=INK)
    face = hub - 5 * s
    picture = None
    if logo:
        try:
            picture = Image.open(logo).convert("RGB")
        except OSError:
            picture = None
    if picture is not None:
        crop = min(picture.size)
        left, top = (picture.width - crop) // 2, (picture.height - crop) // 2
        picture = picture.crop((left, top, left + crop, top + crop)).resize(
            (face * 2, face * 2), Image.Resampling.LANCZOS
        )
        mask = Image.new("L", (face * 2, face * 2), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, face * 2 - 1, face * 2 - 1), fill=255)
        image.paste(picture, (cx - face, cy - face), mask)
    else:
        draw.ellipse((cx - face, cy - face, cx + face, cy + face), fill=NIGHT)
        _sparkle(draw, cx, cy, face * 0.8, LEMON, 0)

    # The pointer, with the same hard shadow as everything else.
    tip, top, half = (CENTER[1] - RADIUS + 22) * s, 6 * s, 20 * s
    for dx, dy, fill in ((4 * s, 4 * s, INK), (0, 0, MAGENTA)):
        draw.polygon(
            [(cx - half + dx, top + dy), (cx + half + dx, top + dy), (cx + dx, tip + dy)],
            fill=fill, outline=INK, width=3 * s,
        )  # fmt: skip

    if brand:
        font = _font(25 * s)
        text = brand[:16]
        width = int(draw.textlength(text, font=font)) + 36 * s
        height = 42 * s
        plaque = Image.new("RGBA", (width + 12 * s, height + 12 * s), (0, 0, 0, 0))
        pen = ImageDraw.Draw(plaque)
        pen.rectangle((6 * s, 6 * s, width + 6 * s, height + 6 * s), fill=INK)
        pen.rectangle((0, 0, width, height), fill=VIOLET, outline=INK, width=3 * s)
        pen.text((width // 2, height // 2), text, font=font, anchor="mm", fill=WHITE)
        plaque = plaque.rotate(3, resample=Image.Resampling.BICUBIC, expand=True)
        image.alpha_composite(plaque, (cx - plaque.width // 2, (HEIGHT - 8) * s - plaque.height))
    return image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def _dim(count: int, winner: int, offset: float):
    """A dark veil over the stopped wheel with a hole where the winning sector is."""
    from PIL import Image, ImageDraw

    s = SCALE
    step = 360 / sectors(count)
    start = 270 - (0.5 + offset) * step  # where the winning sector begins once the wheel stops
    cx, cy, radius = CENTER[0] * s, CENTER[1] * s, RADIUS * s
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    mask = Image.new("L", (WIDTH * s, HEIGHT * s), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(box, fill=150)
    draw.pieslice(box, start, start + step, fill=0)
    veil = Image.new("RGBA", mask.size, INK + (0,))
    veil.putalpha(mask)
    ImageDraw.Draw(veil).pieslice(box, start, start + step, outline=LEMON + (255,), width=4 * s)
    return veil.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def render(
    titles: list[str], winner: int, offset: float = 0.0, logo: str | None = None, brand: str = ""
) -> bytes:
    """GIF bytes. `winner` is an index into `titles`."""
    from PIL import Image

    backdrop = _backdrop().convert("RGBA")
    wheel = _wheel(titles)
    overlay = _overlay(logo, brand)
    final = stop_angle(len(titles), winner, offset)
    corner = (CENTER[0] - wheel.width // 2, CENTER[1] - wheel.height // 2)

    def frame_at(progress: float, veil=None):
        eased = 1 - (1 - progress) ** 3  # fast start, slow stop
        frame = backdrop.copy()
        frame.alpha_composite(
            wheel.rotate(-final * eased, resample=Image.Resampling.BICUBIC), corner
        )
        if veil is not None:
            frame.alpha_composite(veil)
        frame.alpha_composite(overlay)
        return frame.convert("RGB")

    stopped = frame_at(1.0)
    dimmed = frame_at(1.0, _dim(len(titles), winner, offset))
    # One palette for all frames keeps colours steady and the file small; frames are reduced
    # to it as they are made, so the full-colour ones never pile up in memory.
    sample = Image.new("RGB", (WIDTH * 2, HEIGHT))
    sample.paste(stopped, (0, 0))
    sample.paste(dimmed, (WIDTH, 0))
    palette = sample.quantize(colors=96, dither=Image.Dither.NONE)

    def reduce(frame):
        return frame.quantize(palette=palette, dither=Image.Dither.NONE)

    frames = [reduce(frame_at(number / (FRAMES - 1))) for number in range(FRAMES - 1)]
    lit, dark = reduce(stopped), reduce(dimmed)
    frames += [lit, dark, lit, dark, lit, dark]
    out = io.BytesIO()
    frames[0].save(
        out, format="GIF", save_all=True, append_images=frames[1:], loop=0,
        duration=[FRAME_MS] * (FRAMES - 1) + list(BLINK_MS) + [HOLD_MS], optimize=False,
    )  # fmt: skip
    return out.getvalue()
