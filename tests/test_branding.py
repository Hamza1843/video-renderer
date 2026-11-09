"""Tests for the optional branding/logo overlay (issue #4), one per
acceptance criterion.  Run:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import render  # noqa: E402

W, H = render.W, render.H
THEME = render.presets.dark
LW, LH, BORDER = 216, 108, 20   # test logo: exactly 10% of frame height


class BrandingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        # Opaque red core inside a fully transparent border (alpha = 0).
        arr = np.zeros((LH, LW, 4), dtype=np.uint8)
        arr[BORDER:LH - BORDER, BORDER:LW - BORDER] = (255, 0, 0, 255)
        Image.fromarray(arr, "RGBA").save(cls.dir / "logo.png")
        cls.scene = render.build_outro({"type": "outro", "text": "Thanks"}, THEME).with_duration(2)
        cls.plain = cls.scene.get_frame(1).astype(int)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def branded_frame(self, stype="outro", **cfg):
        brand = render.load_branding({"logo": "logo.png", **cfg}, self.dir)
        return brand, render.apply_branding(self.scene, stype, brand).get_frame(1).astype(int)

    def test_four_corners_at_margin(self):
        expected = {"top-left": (40, 40), "top-right": (W - LW - 40, 40),
                    "bottom-left": (40, H - LH - 40), "bottom-right": (W - LW - 40, H - LH - 40)}
        for pos, (x, y) in expected.items():
            with self.subTest(position=pos):
                _, f = self.branded_frame(position=pos, margin=40)
                ys, xs = np.nonzero(np.any(f != self.plain, axis=2))
                # exactly the logo's opaque core changed, at the requested margin
                self.assertEqual((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1),
                                 (x + BORDER, y + BORDER, x + LW - BORDER, y + LH - BORDER))

    def test_opacity_scale_and_alpha(self):
        brand, full = self.branded_frame(opacity=1.0)
        cx, cy = W - 48 - LW // 2, H - 48 - LH // 2           # default corner + margin
        self.assertEqual(tuple(full[cy, cx]), (255, 0, 0))
        _, half = self.branded_frame(opacity=0.5)
        np.testing.assert_allclose(half[cy, cx], 0.5 * np.array([255, 0, 0]) + 0.5 * self.plain[cy, cx], atol=1.5)
        # PNG alpha respected: transparent border pixels keep the background
        corner = (H - 48 - LH + 5, W - 48 - LW + 5)
        np.testing.assert_array_equal(full[corner], self.plain[corner])
        # scale: max_height is a fraction of frame height, aspect ratio kept
        small, _ = self.branded_frame(max_height=0.05)
        self.assertEqual((small[0].w, small[0].h), (LW // 2, LH // 2))

    def test_scenes_filter(self):
        brand = render.load_branding({"logo": "logo.png", "scenes": ["title", "outro"]}, self.dir)
        self.assertIsNot(render.apply_branding(self.scene, "outro", brand), self.scene)
        self.assertIs(render.apply_branding(self.scene, "bullets", brand), self.scene)
        everywhere = render.load_branding({"logo": "logo.png"}, self.dir)  # default "all"
        self.assertIsNot(render.apply_branding(self.scene, "bullets", everywhere), self.scene)

    def test_no_branding_block_leaves_render_unchanged(self):
        self.assertIsNone(render.load_branding(None, self.dir))
        self.assertIs(render.apply_branding(self.scene, "outro", None), self.scene)
        # and the wrapper itself never moves the scene: invisible logo == unbranded
        from moviepy import concatenate_videoclips
        title = render.build_title({"type": "title", "text": "Hi"}, THEME)
        brand = render.load_branding({"logo": "logo.png", "opacity": 0.0}, self.dir)
        for anim in ("fade", "slide-up", "zoom-in"):
            with self.subTest(anim=anim):
                c = render.apply_anim(title.with_duration(2), anim, 2)
                a = concatenate_videoclips([c], method="compose").get_frame(0.3)
                b = concatenate_videoclips([render.apply_branding(c, "title", brand)],
                                           method="compose").get_frame(0.3)
                np.testing.assert_array_equal(a, b)


if __name__ == "__main__":
    unittest.main()
