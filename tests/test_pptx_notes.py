"""Speaker notes in the PPTX export (issue #7).

Run: python -m unittest discover -s tests
"""
import copy
import sys
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pptx import Presentation  # noqa: E402

import pptx_export  # noqa: E402

SPEC = {
    "theme": {"bg": "#0d1117", "fg": "#f0f6fc", "accent": "#58a6ff"},
    "timeline": [
        {"type": "title", "text": "Quarterly Review",
         "narration": "Welcome to the quarterly review."},
        {"type": "bullets", "items": ["Revenue up", "Churn down"],
         "narration_per_bullet": ["Revenue grew.", "Churn fell."]},
        {"type": "outro", "text": "Thank you", "duration": 2},
    ],
}


class SpeakerNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        wav = self.dir / "narr.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 4000)
        self.arts = [{"narration_path": str(wav)}, {}, {}]

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, spec, name="deck.pptx"):
        out = self.dir / name
        pptx_export.build_pptx(spec, self.arts, str(out))
        return out

    def test_scenes_with_narration_have_it_in_notes(self):
        slides = list(Presentation(str(self._build(SPEC))).slides)
        self.assertEqual(slides[0].notes_slide.notes_text_frame.text,
                         "Welcome to the quarterly review.")
        self.assertEqual(slides[1].notes_slide.notes_text_frame.text,
                         "Revenue grew.\nChurn fell.")

    def test_scene_without_narration_has_no_notes(self):
        outro = list(Presentation(str(self._build(SPEC))).slides)[2]
        self.assertFalse(outro.has_notes_slide)

    def test_slides_and_audio_unchanged_by_notes(self):
        no_narration = copy.deepcopy(SPEC)
        for scene in no_narration["timeline"]:
            scene.pop("narration", None)
            scene.pop("narration_per_bullet", None)
        with zipfile.ZipFile(self._build(SPEC, "a.pptx")) as a, \
                zipfile.ZipFile(self._build(no_narration, "b.pptx")) as b:
            parts = [n for n in b.namelist()
                     if n.startswith("ppt/slides/slide") or n.startswith("ppt/media/")]
            self.assertTrue(any(n.startswith("ppt/media/") for n in parts))
            for name in parts:
                self.assertEqual(a.read(name), b.read(name), name)


if __name__ == "__main__":
    unittest.main()
