"""`image` scenes in the PPTX export (issue #14).

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: E402
from pptx.util import Inches  # noqa: E402

import pptx_export  # noqa: E402

THEME = {"bg": "#0d1117", "fg": "#f0f6fc", "accent": "#58a6ff"}
SLIDE_RATIO = pptx_export.SLIDE_W / pptx_export.SLIDE_H


def _pictures(slide):
    return [s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE]


def _texts(slide):
    return [s.text_frame.text for s in slide.shapes if s.has_text_frame and s.text_frame.text]


class ImageSceneExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _img(self, name, size):
        path = self.dir / name
        Image.new("RGB", size, (151, 41, 19)).save(path)
        return str(path)

    def _build(self, timeline, arts):
        out = self.dir / "deck.pptx"
        pptx_export.build_pptx({"theme": THEME, "timeline": timeline}, arts, str(out))
        return list(Presentation(str(out)).slides)

    def assertCoverFit(self, pic, img_w, img_h):
        # full-bleed frame, centred crop, and the visible part of the image has
        # the slide's aspect ratio: cropped like the video, never stretched
        self.assertEqual((pic.left, pic.top, pic.width, pic.height),
                         (0, 0, pptx_export.SLIDE_W, pptx_export.SLIDE_H))
        self.assertAlmostEqual(pic.crop_left, pic.crop_right, places=6)
        self.assertAlmostEqual(pic.crop_top, pic.crop_bottom, places=6)
        vis_w = img_w * (1 - pic.crop_left - pic.crop_right)
        vis_h = img_h * (1 - pic.crop_top - pic.crop_bottom)
        self.assertAlmostEqual(vis_w / vis_h, SLIDE_RATIO, places=3)

    def test_src_picture_and_caption_on_slide(self):
        src = self._img("photo_4x3.png", (400, 300))
        slides = self._build([{"type": "image", "src": src, "caption": "Hello"}],
                             [{"image_png": src}])
        pics = _pictures(slides[0])
        self.assertEqual(len(pics), 1)
        self.assertEqual(pics[0].image.blob, Path(src).read_bytes())
        self.assertCoverFit(pics[0], 400, 300)
        self.assertAlmostEqual(pics[0].crop_top, 0.125, places=3)  # 4:3 on 16:9
        self.assertEqual(_texts(slides[0]), ["Hello"])
        # caption sits on a 60% black bar across the bottom, like the video
        bar = [s for s in slides[0].shapes
               if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and s.top > 0][0]
        self.assertEqual(bar.top + bar.height, pptx_export.SLIDE_H)
        self.assertIn('<a:alpha val="60000"/>', bar._element.xml)
        cap = [s for s in slides[0].shapes if s.has_text_frame and s.text_frame.text][0]
        self.assertGreaterEqual(cap.top, bar.top)

    def test_render_exports_image_scene(self):
        # end to end: render.py must hand the scene's `src` to the exporter
        import json
        import render
        src = self._img("photo_4x3.png", (400, 300))
        spec = self.dir / "spec.json"
        spec.write_text(json.dumps({"meta": {"fps": 5}, "timeline": [
            {"type": "image", "src": src, "caption": "Hello", "duration": 0.4}]}))
        render.render(str(spec), str(self.dir / "out.mp4"), str(self.dir / "cache"))
        slide = Presentation(str(self.dir / "out.pptx")).slides[0]
        pics = _pictures(slide)
        self.assertEqual(len(pics), 1)
        self.assertEqual(pics[0].image.blob, Path(src).read_bytes())
        self.assertEqual(_texts(slide), ["Hello"])

    def test_wide_src_is_cropped_left_right(self):
        src = self._img("photo_wide.png", (600, 200))
        slides = self._build([{"type": "image", "src": src}], [{"image_png": src}])
        pic = _pictures(slides[0])[0]
        self.assertCoverFit(pic, 600, 200)
        self.assertEqual(pic.crop_top, 0)

    def test_other_scene_types_unchanged(self):
        panel = self._img("ai_panel.png", (320, 320))
        slides = self._build(
            [{"type": "title", "text": "Hi"},
             {"type": "bullets", "items": ["a"]},
             {"type": "outro", "text": "Bye"}],
            [{}, {"image_png": panel}, {}])
        self.assertEqual(_texts(slides[0]), ["Hi"])
        # AI image on a bullets scene is still the 4.8in side panel, uncropped
        pic = _pictures(slides[1])[0]
        self.assertEqual((pic.width, pic.height), (Inches(4.8), Inches(4.8)))
        self.assertEqual((pic.crop_left, pic.crop_top), (0, 0))
        self.assertEqual(_texts(slides[2]), ["Bye"])


if __name__ == "__main__":
    unittest.main()
