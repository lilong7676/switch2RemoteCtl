#!/usr/bin/env python3
"""Item price model: quality level -> sale price multiplier.

Source of truth (verified 2026-10-25 against in-game prices):
  GameWith "品質の上げ方と効果" (牧場物語 風のグランドバザール)
  https://gamewith.jp/bokumono-grabaza/515215

Findings from that page:
  * There are exactly 14 quality levels, from 0.5 to 7.0 in 0.5 steps.
  * Quality 7 swaps the star icon to a rainbow colour (matches our badge rule).
  * The sale price multiplier is a fixed step table:
        0.5 .. 3.5   : 1.0 .. 1.3   (+0.05 per step)
        3.5 .. 5.0   : 1.3 .. 1.6   (+0.10 per step)
        5.0 .. 6.0   : 1.6 .. 2.0   (+0.20 per step)
        6.0 .. 7.0   : 2.0 .. 3.0   (+0.50 per step)

Consequence for the automation: one observed price at any quality level is
enough to derive the base price and therefore the price at every level, so the
grid only has to be visited once per item (not once per star variant).
"""

QUALITY_LEVELS = [round(0.5 + 0.5 * i, 1) for i in range(14)]

PRICE_MULTIPLIER = {}
for _i, _q in enumerate(QUALITY_LEVELS):
    if _q <= 3.5:
        _m = 1.0 + 0.05 * _i
    elif _q <= 5.0:
        _m = 1.3 + 0.1 * (_i - 6)
    elif _q <= 6.0:
        _m = 1.6 + 0.2 * (_i - 9)
    else:
        _m = 2.0 + 0.5 * (_i - 11)
    PRICE_MULTIPLIER[_q] = round(_m, 4)

# OCR drops the decimal point ("05" for 0.5), so map digit strings to levels.
DIGIT_VOCAB = {str(q).replace(".", ""): q for q in QUALITY_LEVELS}
DIGIT_VOCAB.update({f"{q:.1f}".replace(".", ""): q for q in QUALITY_LEVELS})
# explicit aliases for the half steps
DIGIT_VOCAB.update({"05": 0.5, "15": 1.5, "25": 2.5, "35": 3.5, "45": 4.5,
                    "55": 5.5, "65": 6.5})


def multiplier(star):
    """Sale price multiplier for a quality level (None if unknown)."""
    if star is None:
        return None
    try:
        value = round(float(star) * 2) / 2
    except (TypeError, ValueError):
        return None
    return PRICE_MULTIPLIER.get(value)


def base_price_from(price, star):
    """Derive the 0.5-star base price from one observed (price, quality)."""
    if price is None or star is None:
        return None
    factor = multiplier(star)
    if not factor:
        return None
    return round(float(price) / factor, 2)


def price_at(base, star):
    """Predict the sale price at a quality level from the base price."""
    if base is None or star is None:
        return None
    factor = multiplier(star)
    if not factor:
        return None
    return int(round(float(base) * factor))


def normalize_digits(text):
    """Fix common OCR letter/digit confusions in the badge text."""
    table = str.maketrans({"O": "0", "o": "0", "D": "0", "S": "5", "s": "5",
                           "I": "1", "l": "1", "|": "1", "Z": "2", "z": "2",
                           "B": "8", "g": "9", "q": "9"})
    return str(text).translate(table)


def snap_badge_digits(text):
    """Snap raw badge OCR digits ("05", "25", "451") to a quality level."""
    print_text = "".join(ch for ch in str(text) if ch.isdigit())
    if not print_text:
        return None
    for length in (4, 3, 2, 1):
        candidate = print_text[:length]
        if candidate in DIGIT_VOCAB:
            return DIGIT_VOCAB[candidate]
    return None


if __name__ == "__main__":
    print("quality levels:", QUALITY_LEVELS)
    print("multipliers:", PRICE_MULTIPLIER)
    # sanity: round trips on the values observed in game on 2026-10-25
    samples = [(90, 0.5), (450, 7.0), (1500, 7.0), (162, 7.0), (433, 2.5), (30, 7.0)]
    for price, star in samples:
        base = base_price_from(price, star)
        print(f"  {price:>5}G @ {star}★ -> base {base} -> back {price_at(base, star)}G")
