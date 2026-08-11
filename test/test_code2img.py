import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from pack import code2img


class CodeToImageWidthTests(unittest.TestCase):
    def _make_measure_context(self, font_size: int = 30, scale: int = 2):
        fs = font_size * scale
        font = ImageFont.truetype(
            str(code2img._pick_font(code2img._FONT_REGULAR_CANDIDATES)),
            fs,
        )
        fallback_fonts = code2img._discover_fallback_fonts(fs)
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        return font, fallback_fonts, draw

    def test_measure_text_with_fallback_counts_cjk_width(self):
        font, fallback_fonts, draw = self._make_measure_context()
        text = 'print("中文中文中文中文")'

        self.assertFalse(code2img._font_has_char(font, "中"))

        primary_only = draw.textlength(text, font=font)
        fallback_aware = code2img._measure_text_with_fallback(
            text, font, fallback_fonts, draw
        )

        self.assertGreater(fallback_aware, primary_only)

    def test_render_code_to_image_uses_fallback_aware_line_width(self):
        code = 'print("中文中文中文中文")'
        font_size = 30
        scale = 2
        font, fallback_fonts, draw = self._make_measure_context(
            font_size=font_size,
            scale=scale,
        )

        char_w = draw.textlength("M", font=font)
        lines = code2img._tokenize_lines(code, code2img._get_lexer(code, "python"))
        ln_digits = len(str(len(lines)))
        ln_width = int(char_w * (ln_digits + 2))
        max_line_px = max(
            int(
                code2img._measure_text_with_fallback(
                    "".join(segment[0] for segment in segs),
                    font,
                    fallback_fonts,
                    draw,
                )
            )
            for segs in lines
        )

        pad = 44 * scale
        win_pad_x = 32 * scale
        expected_width = (pad * 2 + win_pad_x * 2 + ln_width + max_line_px) // scale

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "code.png"
            rendered = code2img.render_code_to_image(
                code,
                language="python",
                out_path=out_path,
                font_size=font_size,
                scale=scale,
            )
            with Image.open(rendered) as image:
                self.assertEqual(image.width, expected_width)


if __name__ == "__main__":
    unittest.main()
