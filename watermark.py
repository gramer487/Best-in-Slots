"""Watermark roll/build screenshots with the build disclaimer and server invite."""

import io
import math

from PIL import Image, ImageDraw, ImageFont, ImageOps

DISCLAIMER_LINES = (
    "This roll data relates",
    "to a specific build",
    "and may not be the",
    "best for your build",
)
GOLD = (255, 215, 0, 255)  # gold letters
OUTLINE = (0, 0, 0, 200)  # black outline so the text reads on light screenshots
WHITE = (255, 255, 255, 255)
DIAGONAL_TEXT = "discord.gg/bestinslots"
DIAGONAL_COLOR = (128, 128, 128)  # gray
DIAGONAL_OPACITY = 0.3
# Block geometry as fractions of the image size (tuned on a 946x727 screenshot).
SQUARE_RATIO = 0.3  # of the image height
FONT_HEIGHT_RATIO = 0.0316  # of the image height
LINE_SPACING = 1.5  # times the font size


def _draw_diagonal(
    overlay: Image.Image, text: str, color: tuple[int, int, int], opacity: float
) -> None:
    """Draw the text along the image's diagonal, as large as it fits."""
    width, height = overlay.size
    angle = math.degrees(math.atan2(height, width))
    probe = ImageFont.load_default(size=100)
    box = probe.getbbox(text)
    probe_width, probe_height = box[2] - box[0], box[3] - box[1]
    # Largest size whose rotated bounding box still fits the image. Along the
    # diagonal the rotated box is W*width/diag + H*height/diag wide and
    # W*height/diag + H*width/diag tall.
    diagonal = math.hypot(width, height)
    font_size = max(
        10,
        int(
            min(
                width * diagonal * 100 / (probe_width * width + probe_height * height),
                height * diagonal * 100 / (probe_width * height + probe_height * width),
            )
        ),
    )
    font = ImageFont.load_default(size=font_size)
    box = font.getbbox(text)
    layer = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (-box[0], -box[1]), text, font=font, fill=(*color, round(255 * opacity))
    )
    rotated = layer.rotate(angle, expand=True, resample=Image.BICUBIC)
    overlay.alpha_composite(
        rotated, ((width - rotated.width) // 2, (height - rotated.height) // 2)
    )


def apply_watermark(data: bytes) -> bytes:
    """Return the image as PNG with the 4-line disclaimer in a bottom-right square."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGBA")
    width, height = image.size
    font_size = max(10, round(height * FONT_HEIGHT_RATIO))
    font = ImageFont.load_default(size=font_size)
    stroke_width = max(1, font_size // 16)
    spacing = round(font_size * LINE_SPACING)
    # Draw on a transparent overlay, then composite it over the image.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    _draw_diagonal(overlay, DIAGONAL_TEXT, DIAGONAL_COLOR, DIAGONAL_OPACITY)
    boxes = [
        draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        for line in DISCLAIMER_LINES
    ]
    text_width = max(box[2] - box[0] for box in boxes)
    # The white square hugs the right and bottom edges of the image. Its size stays
    # fixed; only tiny images grow it so the text still fits.
    side = max(round(height * SQUARE_RATIO), text_width + 2)
    draw.rectangle((width - side, height - side, width, height), fill=WHITE)
    right = width - (side - text_width) / 2
    middle = height - side / 2
    for i, line in enumerate(DISCLAIMER_LINES):
        draw.text(
            (right, middle + (i - (len(DISCLAIMER_LINES) - 1) / 2) * spacing),
            line,
            font=font,
            fill=GOLD,
            stroke_width=stroke_width,
            stroke_fill=OUTLINE,
            anchor="rm",
        )
    out = io.BytesIO()
    Image.alpha_composite(image, overlay).save(out, format="PNG")
    return out.getvalue()
