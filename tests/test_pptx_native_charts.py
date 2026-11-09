"""Issue #8: chart scenes export to PPTX as native, editable PowerPoint charts.

Builds decks with pptx_export.build_pptx (no video render, no API calls),
reopens them with python-pptx and checks the chart objects.

    python -m unittest tests.test_pptx_native_charts -v
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "themes"))
import presets  # noqa: E402
import pptx_export  # noqa: E402

BAR = {"type": "chart", "chart_type": "bar", "heading": "MRR ($k)",
       "labels": ["Jul", "Aug", "Sep"], "values": [410, 455, 512]}
LINE = {"type": "chart", "chart_type": "line", "heading": "Guilt",
        "labels": ["A", "B", "C", "D"], "values": [10, 70, 40, 5]}
PIE = {"type": "chart", "chart_type": "pie", "heading": "Share",
       "labels": ["Ours", "Theirs", "Other"], "values": [50, 30, 20]}


def theme_for(preset: str, **overrides) -> dict:
    """Same merge render.load_spec does: preset, then spec overrides."""
    theme = dict(vars(presets)[preset])
    theme.update(overrides)
    return theme


class NativeChartExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.png = str(self.dir / "chart.png")
        Image.new("RGB", (192, 108), "#123456").save(self.png)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, scenes, theme=None):
        """Build a deck and return (reopened Presentation, captured stdout)."""
        spec = {"theme": theme or theme_for("dark"), "timeline": scenes}
        arts = [{"chart_png": self.png} for _ in scenes]
        out = self.dir / "deck.pptx"
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            pptx_export.build_pptx(spec, arts, str(out))
        return Presentation(str(out)), log.getvalue()

    def pictures(self, slide):
        return [s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE]

    def only_chart(self, prs, idx=0):
        slide = prs.slides[idx]
        frames = [s for s in slide.shapes if s.has_chart]
        self.assertEqual(len(frames), 1, "expected exactly one native chart")
        self.assertEqual(type(frames[0]).__name__, "GraphicFrame")
        self.assertEqual(self.pictures(slide), [], "chart PNG should not be placed")
        return frames[0].chart

    def assert_data(self, chart, scene):
        plot = chart.plots[0]
        self.assertEqual(list(plot.categories), scene["labels"])
        self.assertEqual(list(plot.series[0].values), [float(v) for v in scene["values"]])

    # A bar-chart scene exports as a native, editable chart
    def test_bar_is_native_editable_chart(self):
        prs, log = self.build([BAR])
        chart = self.only_chart(prs)
        self.assertEqual(chart.chart_type, XL_CHART_TYPE.COLUMN_CLUSTERED)
        self.assert_data(chart, BAR)
        self.assertEqual(chart.chart_title.text_frame.text, "MRR ($k)")
        # the embedded workbook is what PowerPoint's "Edit Data" opens
        xlsx = chart.part.chart_workbook.xlsx_part
        self.assertIsNotNone(xlsx)
        self.assertGreater(len(xlsx.blob), 0)
        self.assertNotIn("[warn]", log)

    # Chart colours follow the spec theme (dark background, accent series)
    def assert_themed(self, prs, theme):
        bg, fg, accent = (RGBColor.from_string(theme[k].lstrip("#"))
                          for k in ("bg", "fg", "accent"))
        chart = self.only_chart(prs)
        self.assertEqual(chart.plots[0].series[0].format.fill.fore_color.rgb, accent)
        self.assertEqual(chart.font.color.rgb, fg)
        self.assertEqual(chart.category_axis.tick_labels.font.color.rgb, fg)
        self.assertEqual(chart.value_axis.tick_labels.font.color.rgb, fg)
        # chart and plot areas are transparent over the theme-bg slide
        self.assertEqual(prs.slides[0].shapes[0].fill.fore_color.rgb, bg)
        c_chart = chart._chartSpace.find(qn("c:chart"))
        for parent in (chart._chartSpace, c_chart.find(qn("c:plotArea"))):
            self.assertIsNotNone(parent.find(qn("c:spPr")).find(qn("a:noFill")))

    def test_dark_theme_colours(self):
        theme = theme_for("dark", accent="#c9a227")
        self.assert_themed(self.build([BAR], theme)[0], theme)

    def test_light_theme_colours(self):
        theme = theme_for("light")
        self.assert_themed(self.build([BAR], theme)[0], theme)

    # line and pie work natively...
    def test_line_is_native(self):
        chart = self.only_chart(self.build([LINE])[0])
        self.assertEqual(chart.chart_type, XL_CHART_TYPE.LINE_MARKERS)
        self.assert_data(chart, LINE)

    def test_pie_is_native(self):
        theme = theme_for("dark")
        chart = self.only_chart(self.build([PIE], theme)[0])
        self.assertEqual(chart.chart_type, XL_CHART_TYPE.PIE)
        self.assert_data(chart, PIE)
        colours = [p.format.fill.fore_color.rgb for p in chart.plots[0].series[0].points]
        self.assertEqual(colours[0], RGBColor.from_string(theme["accent"][1:]))

    # ...or fall back to PNG with a warning
    def test_failure_falls_back_to_png_with_warning(self):
        prs, log = self.build([dict(BAR, chart_type="scatter")])
        slide = prs.slides[0]
        self.assertFalse(any(s.has_chart for s in slide.shapes))
        self.assertEqual(len(self.pictures(slide)), 1)
        self.assertIn("[warn] native pptx chart failed", log)

    def test_every_sample_chart_scene_exports_natively(self):
        found = 0
        for path in sorted((ROOT / "samples").glob("*.json")):
            spec = json.loads(path.read_text())
            overrides = {k: v for k, v in spec.get("theme", {}).items() if k != "preset"}
            theme = theme_for(spec.get("theme", {}).get("preset", "dark"), **overrides)
            charts = [s for s in spec["timeline"] if s["type"] == "chart"]
            if not charts:
                continue
            prs, log = self.build(charts, theme)
            self.assertNotIn("[warn]", log, path.name)
            for i, scene in enumerate(charts):
                with self.subTest(sample=path.name, scene=i):
                    chart = self.only_chart(prs, i)
                    self.assertEqual(chart.chart_type,
                                     pptx_export.NATIVE_CHART_TYPES[scene["chart_type"]])
                    self.assert_data(chart, scene)
                    found += 1
        self.assertGreaterEqual(found, 4)  # bar x3 + line x1 across samples/


if __name__ == "__main__":
    unittest.main()
