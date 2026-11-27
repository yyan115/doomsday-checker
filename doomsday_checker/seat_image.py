"""Render a seat availability snapshot for Telegram."""

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from .models import SeatMap, SeatPair

BACKGROUND = "#121a29"
TEXT = "#eff3f9"
AVAILABLE = "#197354"
UNAVAILABLE = "#4d596e"
PAIR = "#ffd479"


def render_seat_map(layout: SeatMap, pair: SeatPair | None, checked_at: str) -> bytes:
    xs = [seat.x for seat in layout.seats]
    ys = [seat.y for seat in layout.seats]
    left = min(xs)
    span_x = max(xs) - left
    span_y = max(ys) - min(ys)
    seat_width = min((seat.max_gap / 1.5 for seat in layout.seats if seat.max_gap > 0), default=1)
    scale = min(43 / seat_width, 1300 / (span_x + seat_width), 900 / (span_y + seat_width))
    width = max(540, round(span_x * scale) + 132)
    height = round(span_y * scale) + 232
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    label_font = ImageFont.load_default(size=16)
    seat_font = ImageFont.load_default(size=max(8, min(15, int(seat_width * scale / 3))))
    row_font = ImageFont.load_default(size=13)

    draw.text((width / 2, 25), "SCREEN", font=label_font, fill=TEXT, anchor="mt")
    draw.rounded_rectangle((width * 0.18, 52, width * 0.82, 59), radius=3, fill="#859abb")

    highlight = {pair.first, pair.second} if pair else set()
    row_positions = {}
    center_x = (width - span_x * scale) / 2
    for seat in layout.seats:
        x = center_x + (seat.x - left) * scale
        y = 103 + seat.row_fraction * span_y * scale
        row_positions.setdefault(seat.row, y)
        half_width = max(2, min(21, seat.max_gap * scale * 0.27))
        half_height = max(2, half_width * 0.69)
        selected = seat.available and seat.label in highlight
        fill = PAIR if selected else AVAILABLE if seat.available else UNAVAILABLE
        draw.rounded_rectangle(
            (x - half_width, y - half_height, x + half_width, y + half_height),
            radius=min(5, int(half_height)),
            fill=fill,
        )
        if seat_font.getlength(seat.label) <= 2 * half_width - 3:
            draw.text(
                (x, y),
                seat.label,
                font=seat_font,
                fill=BACKGROUND if selected else TEXT,
                anchor="mm",
            )

    for label, y in row_positions.items():
        draw.text((24, y), label, font=row_font, fill=TEXT, anchor="mm")
        draw.text((width - 24, y), label, font=row_font, fill=TEXT, anchor="mm")

    x = 26
    legend = [(AVAILABLE, "Available"), (UNAVAILABLE, "Unavailable")]
    if pair:
        legend.append((PAIR, "Highlighted pair"))
    for color, label in legend:
        draw.rounded_rectangle((x, height - 77, x + 16, height - 63), radius=3, fill=color)
        draw.text((x + 22, height - 70), label, font=label_font, fill=TEXT, anchor="lm")
        x += round(label_font.getlength(label)) + 44
    draw.text((26, height - 41), f"Checked {checked_at}", font=row_font, fill="#b7c5dc")

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
