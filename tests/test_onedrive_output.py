"""Issue #19: encode into a local temp dir, then move the result to the output.

Run with:  python -m unittest discover -s tests -v

The MoviePy encoder and the PPTX builder are faked, so no FFmpeg is needed.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import render  # noqa: E402

EMPTY_SPEC = {"theme": {}, "meta": {"fps": 30}, "timeline": []}


class OneDriveOutputTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="render-test-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.project = self.base / "project"       # stands in for the repo / cwd
        self.project.mkdir()
        old_cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, old_cwd)
        self.writes = []                           # (filename, temp_audiofile)

        fake_pptx = types.SimpleNamespace(
            build_pptx=lambda spec, arts, path: Path(path).write_bytes(b"pptx"))
        for patcher in (
            mock.patch.object(render, "load_spec", return_value=EMPTY_SPEC),
            mock.patch.object(render, "concatenate_videoclips",
                              return_value=mock.Mock(write_videofile=self._fake_write)),
            mock.patch.dict(sys.modules, {"pptx_export": fake_pptx}),
            mock.patch.object(render, "print", create=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_write(self, filename, *args, temp_audiofile=None, **kwargs):
        """Mimic MoviePy: write the temp audio file, then the MP4."""
        self.writes.append((Path(filename), temp_audiofile))
        Path(temp_audiofile or "fake-TEMP_MPY_wvf_snd.mp4").write_bytes(b"audio")
        Path(filename).write_bytes(b"mp4")

    def _render(self, output):
        render.render("spec.json", str(output), workdir=str(self.base / "cache"))

    def test_output_inside_onedrive_is_written_once_at_the_end(self):
        # Criterion 1: FFmpeg never writes inside the (slow) OneDrive folder;
        # the finished files are moved there, creating the folder if needed.
        dest = self.base / "OneDrive" / "output" / "demo.mp4"
        self._render(dest)

        (written, _), = self.writes
        self.assertNotIn(dest.parent.parent, written.parents)
        self.assertEqual(dest.read_bytes(), b"mp4")
        self.assertEqual(dest.with_suffix(".pptx").read_bytes(), b"pptx")

    def test_no_temp_audio_or_video_left_in_project_dir(self):
        # Criterion 2: MoviePy's temp audio goes into the temp dir, not the cwd.
        self._render(self.project / "output" / "demo.mp4")

        (written, temp_audio), = self.writes
        self.assertEqual(Path(temp_audio).parent, written.parent)
        leftovers = sorted(str(p.relative_to(self.project))
                           for p in self.project.rglob("*") if p.is_file())
        self.assertEqual(leftovers, ["output/demo.mp4", "output/demo.pptx"])
        self.assertFalse(written.parent.exists())   # temp dir removed on success

    def test_temp_dir_removed_when_render_fails(self):
        # Criterion 3: the temp dir is cleaned up even if encoding fails.
        def broken_write(filename, *args, **kwargs):
            self.writes.append((Path(filename), kwargs.get("temp_audiofile")))
            Path(filename).write_bytes(b"half-written")
            raise BrokenPipeError(32, "Broken pipe")

        dest = self.project / "output" / "demo.mp4"
        render.concatenate_videoclips.return_value.write_videofile = broken_write
        with self.assertRaises(BrokenPipeError):
            self._render(dest)

        (written, _), = self.writes
        self.assertFalse(written.parent.exists())
        self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
