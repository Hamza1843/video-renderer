"""Tests for PowerPoint-only render mode (--pptx-only / render_video=False).

Run with:  python3 -m unittest discover -s tests -v

No API key is needed: narration WAVs and scene images are pre-seeded into
the workdir under the cache filenames the renderer looks up, so the runs
exercise the warm-cache path and any API call is a test failure.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import images as imggen  # noqa: E402
import pptx_export  # noqa: E402
import render  # noqa: E402
import tts  # noqa: E402

NO_KEY_ENV = {k: v for k, v in os.environ.items() if k != "MISTRAL_API_KEY"}
IMG_RIGHT = {"prompt": "test panel image", "placement": "right"}
IMG_BG = {"prompt": "test background image", "placement": "background", "opacity": 0.3}


def _spec(fps=30):
    return {
        "meta": {"title": "pptx-only test", "fps": fps},
        "theme": {"preset": "dark", "accent": "#58a6ff"},
        "voice": {"slug": "test"},
        "timeline": [
            {"type": "title", "text": "Hello", "subtitle": "Sub",
             "narration": "Welcome to the test deck."},
            {"type": "bullets", "heading": "Panel", "items": ["a", "b"],
             "narration": "Bullets with a side image.", "image": IMG_RIGHT},
            {"type": "chart", "chart_type": "bar", "heading": "Chart",
             "labels": ["x", "y"], "values": [1, 2],
             "narration": "A small bar chart."},
            {"type": "bullets", "heading": "Background", "items": ["c"],
             "image": IMG_BG},
            {"type": "outro", "text": "Bye", "duration": 0.4},
        ],
    }


def _write_wav(path: Path, seconds: float = 0.3, rate: int = 24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def _seed_cache(spec: dict, workdir: Path):
    """Pre-fill narration + scene images exactly where render.py looks."""
    workdir.mkdir(parents=True, exist_ok=True)
    for scene in spec["timeline"]:
        if scene.get("narration"):
            h = hashlib.sha1(scene["narration"].encode()).hexdigest()[:10]
            _write_wav(workdir / f"narr_{h}_0.wav")
        if scene.get("image"):
            h = hashlib.sha1(json.dumps(scene["image"], sort_keys=True).encode()).hexdigest()[:12]
            Image.new("RGB", (64, 64), (200, 80, 60)).save(workdir / f"sceneimg_{h}.png")


def _no_api(*_a, **_k):
    raise AssertionError("API called although the cache was warm")


def _video_forbidden(*_a, **_k):
    raise AssertionError("video clip/encoding code ran in pptx-only mode")


def _slide_summary(path):
    """Per slide: (number of embedded audio/video shapes, number of pictures)."""
    out = []
    for slide in Presentation(path).slides:
        types = [s.shape_type for s in slide.shapes]
        out.append((types.count(MSO_SHAPE_TYPE.MEDIA), types.count(MSO_SHAPE_TYPE.PICTURE)))
    return out


class PptxOnlyRenderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.workdir = self.tmp / "cache"
        self.out_dir = self.tmp / "out"
        self.out_dir.mkdir()
        env = mock.patch.dict(os.environ, NO_KEY_ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _write_spec(self, spec, name="spec.json"):
        p = self.tmp / name
        p.write_text(json.dumps(spec))
        return p

    def _render_pptx_only(self, spec, output="deck.pptx"):
        spec_path = self._write_spec(spec)
        out = self.out_dir / output
        no_video = {name: _video_forbidden for name in (
            "ColorClip", "CompositeVideoClip", "ImageClip", "TextClip",
            "VideoFileClip", "concatenate_videoclips", "CompositeAudioClip")}
        with mock.patch.multiple(render, **no_video), \
                mock.patch.object(render.subprocess, "run", _video_forbidden), \
                mock.patch.object(tts, "synth_to_file", _no_api), \
                mock.patch.object(imggen, "generate_image", _no_api):
            render.render(str(spec_path), str(out), str(self.workdir), render_video=False)
        return out.with_suffix(".pptx")

    def test_deck_is_complete_and_no_video_is_written(self):
        spec = _spec()
        _seed_cache(spec, self.workdir)
        deck = self._render_pptx_only(spec)

        self.assertTrue(deck.exists())
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()), ["deck.pptx"])
        self.assertFalse((self.workdir / "segments").exists())
        # (audio shapes, pictures) per slide: narration on slides 0-2,
        # side-panel image on 1, chart PNG on 2, background image on 3.
        self.assertEqual(_slide_summary(deck), [(1, 0), (1, 1), (1, 1), (0, 1), (0, 0)])

    def test_long_timeline_skips_segment_streaming(self):
        # >12 scenes switches the video path to per-scene segments + ffmpeg
        # concat; pptx-only must not create segments or call ffmpeg.
        spec = _spec()
        spec["timeline"] = [{"type": "title", "text": f"Scene {i}"} for i in range(31)]
        deck = self._render_pptx_only(spec)
        self.assertEqual(len(Presentation(deck).slides), 31)
        self.assertFalse((self.workdir / "segments").exists())

    def test_artifacts_match_full_render(self):
        """The deck inputs are the same whether or not video is rendered."""
        spec = _spec(fps=2)
        for scene in spec["timeline"]:
            scene["anim"] = "none"
        _seed_cache(spec, self.workdir)
        spec_path = self._write_spec(spec)
        captured = {}

        def capture(mode):
            def _build(spec_, artifacts, path):
                captured[mode] = artifacts
                return path
            return _build

        with mock.patch.object(tts, "synth_to_file", _no_api), \
                mock.patch.object(imggen, "generate_image", _no_api):
            with mock.patch.object(pptx_export, "build_pptx", capture("video")):
                render.render(str(spec_path), str(self.out_dir / "full.mp4"), str(self.workdir))
            with mock.patch.object(pptx_export, "build_pptx", capture("pptx")):
                render.render(str(spec_path), str(self.out_dir / "deck.pptx"),
                              str(self.workdir), render_video=False)

        self.assertTrue((self.out_dir / "full.mp4").exists())
        self.assertFalse((self.out_dir / "deck.mp4").exists())
        self.assertEqual(len(captured["pptx"]), len(spec["timeline"]))
        self.assertEqual(captured["pptx"], captured["video"])


class CliTest(unittest.TestCase):
    def _run(self, *args, cwd):
        return subprocess.run([sys.executable, str(ROOT / "src" / "render.py"), *args],
                              cwd=cwd, env=NO_KEY_ENV, capture_output=True, text=True,
                              timeout=120)

    def test_pptx_only_flag(self):
        with tempfile.TemporaryDirectory() as d:
            spec = Path(d) / "spec.json"
            spec.write_text(json.dumps({"timeline": [
                {"type": "title", "text": "CLI"},
                {"type": "chart", "chart_type": "pie", "labels": ["a", "b"], "values": [1, 3]},
                {"type": "outro", "text": "End"}]}))
            r = self._run(str(spec), "-o", "out/deck.pptx", "--pptx-only",
                          "--workdir", "cache", cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("[scene 2] outro", r.stdout)  # per-scene timing still logged
            self.assertEqual(os.listdir(Path(d) / "out"), ["deck.pptx"])
            self.assertEqual(_slide_summary(Path(d) / "out" / "deck.pptx"),
                             [(0, 0), (0, 1), (0, 0)])


if __name__ == "__main__":
    unittest.main()
