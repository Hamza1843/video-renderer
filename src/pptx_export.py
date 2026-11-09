"""Build a .pptx from the render artifacts: one slide per scene, with each
scene's narration WAV embedded on its slide.

Built from the individual artifacts (spec, narration clips, chart PNGs,
generated scene images) — never from the rendered video.

Chart scenes become native, editable PowerPoint charts; the video's chart PNG
is only used as a fallback.
"""
import json
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_MARKER_STYLE
from pptx.enum.text import PP_ALIGN
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn
from pptx.util import Emu, Inches, Pt

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)


def _rgb(hex_color: str) -> RGBColor:
    h = hex_color.lstrip("#")
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _blend(fg: RGBColor, bg: RGBColor, alpha: float) -> RGBColor:
    """`fg` drawn at `alpha` opacity over `bg` (matplotlib's faint grid)."""
    return RGBColor(*(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg)))


NATIVE_CHART_TYPES = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
}

# Same frame the chart PNG occupies.
CHART_X, CHART_Y = Inches(0.8), Inches(0.8)
CHART_W, CHART_H = SLIDE_W - Inches(1.6), SLIDE_H - Inches(1.6)

_NO_FILL_SPPR = "<c:spPr %s><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>" % nsdecls("c", "a")


def _clear_background(chart):
    """Transparent chart and plot areas so the slide's theme background shows
    through, as in the video. python-pptx has no API for these."""
    c_chart = chart._chartSpace.find(qn("c:chart"))
    c_chart.addnext(parse_xml(_NO_FILL_SPPR))                  # spPr follows c:chart
    c_chart.find(qn("c:plotArea")).append(parse_xml(_NO_FILL_SPPR))


def _style_text(text_frame, text: str, size: int, color: RGBColor, bold=False):
    text_frame.text = text
    font = text_frame.paragraphs[0].runs[0].font
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = color


def add_native_chart(slide, scene: dict, bg: RGBColor, fg: RGBColor, accent: RGBColor):
    """Editable chart from the scene's labels/values, themed like the
    matplotlib chart in the video."""
    kind = scene.get("chart_type", "bar")
    data = CategoryChartData()
    data.categories = scene["labels"]
    data.add_series(scene.get("y_label") or scene.get("heading") or "Value", scene["values"])
    frame = slide.shapes.add_chart(NATIVE_CHART_TYPES[kind], CHART_X, CHART_Y,
                                   CHART_W, CHART_H, data)
    chart = frame.chart
    try:
        _style_chart(chart, scene, kind, bg, fg, accent)
    except Exception:
        frame._element.getparent().remove(frame._element)  # no half-styled chart
        raise
    return frame


def _style_chart(chart, scene, kind, bg, fg, accent):
    _clear_background(chart)
    chart.font.size = Pt(14)
    chart.font.color.rgb = fg
    chart.has_legend = False
    if scene.get("heading"):
        chart.has_title = True
        _style_text(chart.chart_title.text_frame, scene["heading"], 24, fg, bold=True)
    else:
        chart.has_title = False

    plot = chart.plots[0]
    series = plot.series[0]
    if kind == "pie":
        # as in the video: accent/fg/grey slices, category + percent labels
        palette = [accent, fg, RGBColor(0x88, 0x88, 0x88)]
        for i, point in enumerate(series.points):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = palette[i % len(palette)]
            point.format.line.color.rgb = bg
        labels = series.data_labels
        labels.font.color.rgb = fg
        labels.show_category_name = True
        labels.show_percentage = True
        labels.show_value = False
        labels.number_format = "0%"
        labels.number_format_is_linked = False
        labels.position = XL_LABEL_POSITION.OUTSIDE_END
        return

    plot.vary_by_categories = False
    if kind == "bar":
        plot.gap_width = 80
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = accent
    else:  # line
        series.smooth = False
        series.format.line.color.rgb = accent
        series.format.line.width = Pt(3)
        series.marker.style = XL_MARKER_STYLE.CIRCLE
        series.marker.size = 10
        series.marker.format.fill.solid()
        series.marker.format.fill.fore_color.rgb = accent
        series.marker.format.line.color.rgb = accent

    for axis in (chart.category_axis, chart.value_axis):
        axis.format.line.color.rgb = fg
        axis.tick_labels.font.color.rgb = fg
    chart.value_axis.has_major_gridlines = True
    chart.value_axis.major_gridlines.format.line.color.rgb = _blend(fg, bg, 0.25)
    if scene.get("y_label"):
        chart.value_axis.has_title = True
        _style_text(chart.value_axis.axis_title.text_frame, scene["y_label"], 16, fg)


def _add_chart_scene(slide, scene: dict, art: dict, bg, fg, accent):
    """Native chart; falls back to the video's chart PNG with a warning if
    the native chart can't be built."""
    try:
        add_native_chart(slide, scene, bg, fg, accent)
        return
    except Exception as e:
        print(f"[warn] native pptx chart failed ({type(e).__name__}: {e}); using chart PNG")
    if art.get("chart_png"):
        slide.shapes.add_picture(art["chart_png"], left=CHART_X, top=CHART_Y,
                                 width=CHART_W, height=CHART_H)


def build_pptx(spec: dict, scene_artifacts: list, output_path: str):
    """scene_artifacts: per-scene dicts with optional keys
    narration_path, chart_png, image_png, bg_image_png, scrim (0-1)."""
    theme = spec.get("theme", {})
    bg = _rgb(theme.get("bg", "#0d1117"))
    fg = _rgb(theme.get("fg", "#f0f6fc"))
    accent = _rgb(theme.get("accent", "#58a6ff"))

    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H
    blank = prs.slide_layouts[6]

    for scene, art in zip(spec["timeline"], scene_artifacts):
        slide = prs.slides.add_slide(blank)

        # background fill (or faded image background)
        if art.get("bg_image_png"):
            slide.shapes.add_picture(art["bg_image_png"], 0, 0,
                                     width=SLIDE_W, height=SLIDE_H)
            scrim = art.get("scrim", 0.5)
            box = slide.shapes.add_shape(1, 0, 0, SLIDE_W, SLIDE_H)  # rectangle
            box.fill.solid()
            box.fill.fore_color.rgb = bg
            box.fill.transparency = 1.0 - scrim  # pptx: transparency of the fill
            box.line.fill.background()
        else:
            box = slide.shapes.add_shape(1, 0, 0, SLIDE_W, SLIDE_H)
            box.fill.solid()
            box.fill.fore_color.rgb = bg
            box.line.fill.background()

        stype = scene["type"]
        if stype in ("title", "outro"):
            tb = slide.shapes.add_textbox(Inches(1), Inches(2.6),
                                          SLIDE_W - Inches(2), Inches(1.5))
            tf = tb.text_frame
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run()
            r.text = scene["text"]
            f = r.font
            f.size = Pt(scene.get("_title_pt", 44))
            f.bold = True
            f.color.rgb = accent if stype == "outro" else fg
            if scene.get("subtitle"):
                p2 = tf.add_paragraph()
                p2.alignment = PP_ALIGN.CENTER
                r2 = p2.add_run()
                r2.text = scene["subtitle"]
                r2.font.size = Pt(20)
                r2.font.color.rgb = accent
        elif stype == "bullets":
            has_panel = art.get("image_png")
            text_w = Inches(5.6) if has_panel else SLIDE_W - Inches(2.4)
            tx = Inches(1.2)
            if scene.get("heading"):
                hb = slide.shapes.add_textbox(tx, Inches(0.7), text_w, Inches(0.9))
                hp = hb.text_frame.paragraphs[0]
                hr = hp.add_run()
                hr.text = scene["heading"]
                hr.font.size = Pt(28)
                hr.font.bold = True
                hr.font.color.rgb = accent
            bb = slide.shapes.add_textbox(tx, Inches(1.9), text_w,
                                          SLIDE_H - Inches(2.6))
            btf = bb.text_frame
            for i, item in enumerate(scene.get("items", [])):
                p = btf.paragraphs[0] if i == 0 else btf.add_paragraph()
                p.alignment = PP_ALIGN.LEFT
                r = p.add_run()
                r.text = "\u2022 " + item
                r.font.size = Pt(18)
                r.font.color.rgb = fg
                p.space_after = Pt(12)
            if has_panel:
                slide.shapes.add_picture(
                    art["image_png"],
                    left=SLIDE_W - Inches(5.4), top=Inches(1.2),
                    width=Inches(4.8), height=Inches(4.8))
        elif stype == "chart":
            _add_chart_scene(slide, scene, art, bg, fg, accent)
        elif stype == "image" and art.get("image_png"):
            slide.shapes.add_picture(art["image_png"], 0, 0,
                                     width=SLIDE_W, height=SLIDE_H)
            if scene.get("caption"):
                cb = slide.shapes.add_textbox(
                    Inches(1), SLIDE_H - Inches(1.1),
                    SLIDE_W - Inches(2), Inches(0.7))
                cp = cb.text_frame.paragraphs[0]
                cp.alignment = PP_ALIGN.CENTER
                cr = cp.add_run()
                cr.text = scene["caption"]
                cr.font.size = Pt(16)
                cr.font.color.rgb = fg

        # embed narration audio on the slide
        if art.get("narration_path"):
            slide.shapes.add_movie(
                art["narration_path"],
                left=SLIDE_W - Inches(1.05), top=SLIDE_H - Inches(1.05),
                width=Inches(0.8), height=Inches(0.8),
                poster_frame_image=None, mime_type="audio/wav"
            )

    prs.save(output_path)
    return output_path
