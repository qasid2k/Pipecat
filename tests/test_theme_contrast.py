"""Every text colour in the web app stays readable (WCAG AA, 4.5:1).

The colours live in web/src/theme.css and are meant to be changed -- a rebrand
is a change to those variables and nothing else ([[decisions]] 057). This test
is what makes that safe: change a colour so that some text becomes hard to
read, and it fails naming the exact pair and theme.

It exists because the first draft of the theme claimed AA in a comment and was
wrong twice (white on the dark-mode button was 2.4:1).
"""

import re
import unittest
from pathlib import Path

THEME = Path(__file__).resolve().parent.parent / "web" / "src" / "theme.css"
AA = 4.5

# (text, background) pairs the components actually use.
PAIRS = [
    ("text", "surface"), ("text", "bg"), ("text", "busy-soft"), ("text", "brand-soft"),
    ("muted", "surface"), ("muted", "surface-2"), ("muted", "bg"), ("muted", "busy-soft"),
    ("ok", "ok-soft"), ("busy", "busy-soft"), ("bad", "bad-soft"), ("bad", "surface"),
    ("brand", "brand-soft"), ("on-brand", "brand"),
]


def _vars(css: str) -> tuple[dict, dict]:
    css = css.replace("\r\n", "\n")
    light = dict(re.findall(r"--([\w-]+):\s*([^;]+);", css[css.index(":root {"):css.index("@media")]))
    dark_part = css[css.index("@media (prefers-color-scheme: dark)"):css.index("@theme")]
    return light, dict(re.findall(r"--([\w-]+):\s*([^;]+);", dark_part))


def _rgb(value: str, over: tuple) -> tuple:
    """Hex, or rgba() blended over `over` (translucent tints sit on a surface)."""
    value = value.strip()
    if value.startswith("#"):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b, a = (float(x) for x in re.findall(r"[\d.]+", value))
    return tuple(round(a * c + (1 - a) * o) for c, o in zip((r, g, b), over))


def _luminance(c: tuple) -> float:
    def ch(x):
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return sum(k * ch(x) for k, x in zip((0.2126, 0.7152, 0.0722), c))


def contrast(a: tuple, b: tuple) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class ContrastTest(unittest.TestCase):
    def test_every_pair_passes_aa_in_both_themes(self):
        light, dark = _vars(THEME.read_text(encoding="utf-8"))
        for name, theme in (("light", light), ("dark", dark)):
            surface = _rgb(theme["surface"], (255, 255, 255))
            for fg, bg in PAIRS:
                with self.subTest(theme=name, pair=f"{fg} on {bg}"):
                    ratio = contrast(_rgb(theme[fg], surface), _rgb(theme[bg], surface))
                    self.assertGreaterEqual(
                        ratio, AA, f"{name}: --{fg} on --{bg} is {ratio:.2f}:1, below {AA}:1"
                    )

    def test_the_check_itself_is_right(self):
        # Known values: black on white is 21:1; #777 on white is ~4.48 (fails).
        self.assertAlmostEqual(contrast((0, 0, 0), (255, 255, 255)), 21.0, places=1)
        self.assertLess(contrast((0x77, 0x77, 0x77), (255, 255, 255)), AA)


if __name__ == "__main__":
    unittest.main()
