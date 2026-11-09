"""Declarative video spec renderer.

Usage:
    python3 render.py spec.json [-o output.mp4] [--workdir /tmp/renders]

Scene durations are audio-driven when `narration` is present (duration = audio
length + padding), unless an explicit `duration` overrides them.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from moviepy import (ColorClip, CompositeVideoClip, ImageClip, TextClip,
                     VideoFileClip, AudioFileClip, CompositeAudioClip, concatenate_videoclips,
                     concatenate_audioclips, vfx)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "themes"))
import images as imggen
import tts
import presets

ANIM_IN = 0.6
SLIDE_DIST = 120
W, H = 1920, 1080


def rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _resolve_font(bold: bool = False) -> str:
    """Return a TTF path with wide glyph coverage (incl. U+2022 bullet)."""
    import os
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
        os.path.join(os.path.dirname(matplotlib.__file__),
                     "mpl-data", "fonts", "ttf",
                     "DejaVuSans%s.ttf" % ("-Bold" if bold else "")),
        os.path.join(os.path.dirname(matplotlib.__file__),
                     "mpl-data", "fonts", "ttf", "DejaVuSans.ttf"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise RuntimeError("No suitable TTF font found; install fonts-dejavu or matplotlib")


def load_spec(path: str) -> dict:
    spec = json.loads(Path(path).read_text())
    theme = dict(presets.__dict__[spec.get("theme", {}).get("preset", "dark")])
    theme.update({k: v for k, v in spec.get("theme", {}).items() if k != "preset"})
    spec["theme"] = theme
    spec.setdefault("meta", {})
    spec["meta"].setdefault("resolution", {"width": 1920, "height": 1080})
    spec["meta"].setdefault("fps", 30)
    return spec


def synth_narration(scene: dict, workdir: Path, voice_cfg: dict) -> tuple[str | None, float]:
    """Synthesize narration if present; return (audio_path, duration)."""
    texts = []
    per_bullet = scene.get("narration_per_bullet")
    if per_bullet:
        texts = per_bullet
    elif scene.get("narration"):
        texts = [scene["narration"]]
    if not texts:
        return None, 0.0
    paths = []
    for i, text in enumerate(texts):
        h = hashlib.sha1(text.encode()).hexdigest()[:10]
        out = workdir / f"narr_{h}_{i}.{voice_cfg.get('format', 'wav')}"
        if not out.exists():
            tts.synth_to_file(
                text, str(out),
                voice_slug=voice_cfg.get("slug"), voice_id=voice_cfg.get("voice_id"),
                model=voice_cfg.get("model", "voxtral-mini-tts-2603"),
                fmt=voice_cfg.get("format", "wav"),
            )
        paths.append(str(out))
    if len(paths) == 1:
        return paths[0], tts.audio_duration(paths[0])
    combined = workdir / f"narr_combined_{hashlib.sha1(json.dumps(paths).encode()).hexdigest()[:10]}.{voice_cfg.get('format', 'wav')}"
    if not combined.exists():
        merged = concatenate_audioclips([AudioFileClip(p) for p in paths])
        merged.write_audiofile(str(combined), logger=None)
    return str(combined), tts.audio_duration(str(combined))


def scene_duration(scene: dict, audio_dur: float, default: float = 4.0) -> float:
    explicit = scene.get("duration")
    if audio_dur > 0:
        # Never cut narration short: explicit duration is a floor, not a cap.
        audio_driven = audio_dur + scene.get("padding", 1.0)
        if explicit:
            return max(explicit, audio_driven)
        return audio_driven
    if explicit:
        return explicit
    if scene.get("type") == "bullets":
        return 1.2 + scene.get("stagger", 0.4) * len(scene.get("items", [])) + 2.0
    return default



def centered_text_clip(text: str, theme: dict, size: int, color: str | None = None,
                       x_frac: float = 0.5, y_frac: float = 0.5,
                       max_w_frac: float = 0.85, bold: bool = False) -> TextClip:
    """Text centered at (x_frac, y_frac) of the frame.

    Auto height with a generous bottom margin: the box is always taller
    than the text, so descenders never clip.
    """
    clip = TextClip(
        text=text, font_size=size,
        color=color or theme["fg"],
        font=_resolve_font(bold),
        method="caption",
        text_align="center",
        vertical_align="center",
        size=(int(W * max_w_frac), None),
        margin=(15, 8, 15, int(size * 0.45)),
    )
    cx, cy = int(W * x_frac), int(H * y_frac)
    pos = (cx - clip.w // 2, cy - clip.h // 2)
    return clip.with_position(pos)


def scene_image_clip(scene: dict, theme: dict, workdir: Path):
    """Generate (if needed) and return the scene's AI image path, or None."""
    img_cfg = scene.get("image")
    if not img_cfg:
        return None
    if not img_cfg.get("prompt"):
        print("[warn] scene 'image' block missing 'prompt'; skipping")
        return None
    out = workdir / f"sceneimg_{hashlib.sha1(json.dumps(img_cfg, sort_keys=True).encode()).hexdigest()[:12]}.png"
    if not out.exists():
        try:
            imggen.generate_image(img_cfg["prompt"], str(out),
                                   model=img_cfg.get("model"), workdir=workdir)
        except RuntimeError as e:
            print(f"[warn] image generation failed ({e}); rendering without image")
            return None
    return str(out)


def build_title(scene, theme, workdir=None):
    bg = ColorClip(size=(W, H), color=rgb(theme["bg"]))
    clips = [bg]
    img_path = scene_image_clip(scene, theme, workdir) if workdir else None
    if img_path:
        clips = composite_bg_image(clips, scene, theme, img_path)
    title = centered_text_clip(scene["text"], theme, theme["title_font_size"],
                               bold=True, y_frac=0.42)
    clips.append(title)
    if scene.get("subtitle"):
        sub = centered_text_clip(scene["subtitle"], theme,
                                 int(theme["font_size"] * 0.55),
                                 color=theme["accent"], y_frac=0.58)
        clips.append(sub)
    return CompositeVideoClip(clips, size=(W, H))


def composite_bg_image(clips, scene, theme, img_path):
    """Insert a faded background image behind text with best-effort WCAG scrim."""
    img_cfg = scene["image"]
    opacity = img_cfg.get("opacity", 0.25)
    bg_img = ImageClip(img_path)
    scale = max(W / bg_img.w, H / bg_img.h)
    bg_img = bg_img.resized(scale)
    if bg_img.w > W or bg_img.h > H:
        bg_img = bg_img.cropped(x_center=bg_img.w / 2, y_center=bg_img.h / 2,
                                width=W, height=H)
    bg_img = bg_img.with_opacity(opacity).with_position((0, 0))
    scrim, passes = imggen.scrim_for_contrast(
        img_path, theme["fg"], theme["bg"],
        text_area=(0.5, 0.5, 0.7, 0.4),
        img_opacity=opacity,
    )
    if not passes:
        print(f"[warn] scene background image cannot reach 4.5:1 contrast "
              f"even at scrim {scrim:.2f}; text may be hard to read")
    scrim_clip = ColorClip(size=(W, H), color=rgb(theme["bg"])) \
        .with_opacity(scrim).with_position((0, 0))
    return [clips[0], bg_img, scrim_clip] + clips[1:]


def build_spotlight_panel(scene, theme, img_path):
    """Rounded image panel on one side; returns (clips_to_add, text_x_frac)."""
    placement = scene["image"].get("placement", "right")
    side = placement if placement in ("left", "right") else "right"
    panel_w = int(W * 0.38)
    panel_h = int(H * 0.70)
    x = int(W * 0.56) if side == "right" else int(W * 0.06)
    y = int(H * 0.15)

    img = ImageClip(img_path)
    scale = max(panel_w / img.w, panel_h / img.h)
    img = img.resized(scale)
    if img.w > panel_w or img.h > panel_h:
        img = img.cropped(x_center=img.w / 2, y_center=img.h / 2,
                          width=panel_w, height=panel_h)
    img = img.with_position((x, y))
    border = ColorClip(size=(panel_w + 8, panel_h + 8),
                       color=rgb(theme["accent"])).with_position((x - 4, y - 4))
    text_x = 0.28 if side == "right" else 0.72
    return [border, img], text_x


def _left_text_clip(text: str, theme: dict, size: int, color: str | None = None,
                    bold: bool = False, max_w_px: int | None = None) -> TextClip:
    """Left-aligned text via auto-sized label clips: true edge alignment,
    with margin keeping the box taller than the text (no descender clipping)."""
    font = _resolve_font(bold)
    if max_w_px:
        text = _wrap_to_width(text, size, font, max_w_px)
    return TextClip(
        text=text, font_size=size,
        color=color or theme["fg"],
        font=font, method="label", text_align="left",
        vertical_align="center",
        margin=(0, 2, 0, int(size * 0.45)),
    )


def _wrap_to_width(text: str, size: int, font: str, max_w_px: int,
                   max_lines: int = 3) -> str:
    """Word-wrap so each line renders narrower than max_w_px."""
    from PIL import ImageFont
    f = ImageFont.truetype(font, size)
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if f.getbbox(trial)[2] <= max_w_px or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines[:max_lines])


def build_bullets(scene, theme, workdir=None):
    bg = ColorClip(size=(W, H), color=rgb(theme["bg"]))
    clips = [bg]
    text_x_frac = 0.5
    panel_clips = []
    img_path = scene_image_clip(scene, theme, workdir) if workdir else None
    if img_path:
        img_cfg = scene["image"]
        placement = img_cfg.get("placement", "background")
        if placement in ("left", "right"):
            panel_clips, text_x_frac = build_spotlight_panel(scene, theme, img_path)
            clips.extend(panel_clips)
        else:
            clips = composite_bg_image(clips, scene, theme, img_path)
    max_w = 0.5 if panel_clips else 0.7
    x = int(W * (text_x_frac - max_w / 2))
    if scene.get("heading"):
        h_size = int(theme["font_size"] * 0.8)
        heading = _left_text_clip(scene["heading"], theme, h_size,
                                  color=theme["accent"], bold=True,
                                  max_w_px=int(W * max_w))
        clips.append(heading.with_position((x, int(H * 0.16))))
    n = len(scene["items"])
    b_size = int(theme["font_size"] * 0.55)
    top, bottom = 0.30, 0.85
    step = (bottom - top) / max(n, 1)
    for i, item in enumerate(scene["items"]):
        bullet = _left_text_clip("• " + item, theme, b_size,
                                 max_w_px=int(W * max_w))
        y = int(H * (top + i * step))
        clips.append(bullet.with_position((x, y)))
    return CompositeVideoClip(clips, size=(W, H))


def build_image(scene, theme, workdir):
    src = scene["src"]
    img_clip = ImageClip(src)
    scale = max(W / img_clip.w, H / img_clip.h)
    img_clip = img_clip.resized(scale)
    if img_clip.w > W or img_clip.h > H:
        img_clip = img_clip.cropped(x_center=img_clip.w / 2, y_center=img_clip.h / 2,
                                     width=W, height=H)
    img_clip = img_clip.with_position("center")
    clips = [img_clip]
    if scene.get("caption"):
        cap_h = int(H * 0.12)
        cap = centered_text_clip(scene["caption"], theme,
                                 int(theme["font_size"] * 0.5), y_frac=0.92)
        cap_bg = ColorClip(size=(W, cap_h), color=(0, 0, 0)).with_opacity(0.6)
        cap_bg = cap_bg.with_position((0, H - cap_h))
        clips.extend([cap_bg, cap])
    return CompositeVideoClip(clips, size=(W, H))


def build_chart(scene, theme, workdir):
    fig, ax = plt.subplots(figsize=(19.2, 10.8), dpi=100)
    fig.patch.set_facecolor(theme["bg"])
    ax.set_facecolor(theme["bg"])
    ax.tick_params(colors=theme["fg"], labelsize=18)
    for spine in ax.spines.values():
        spine.set_color(theme["fg"])
    if scene.get("chart_type", "bar") == "bar":
        bars = ax.bar(scene["labels"], scene["values"],
                      color=theme["accent"], width=0.55)
    elif scene["chart_type"] == "line":
        ax.plot(scene["labels"], scene["values"], color=theme["accent"],
                linewidth=4, marker="o", markersize=10)
    else:
        ax.pie(scene["values"], labels=scene["labels"], colors=[theme["accent"], theme["fg"], "#888"],
               textprops={"color": theme["fg"], "fontsize": 18}, autopct="%1.0f%%")
    if scene["chart_type"] != "pie":
        if scene.get("y_label"):
            ax.set_ylabel(scene["y_label"], color=theme["fg"], fontsize=20)
        ax.set_title(scene.get("heading", ""), color=theme["fg"],
                     fontsize=28, pad=20)
        ax.grid(axis="y", alpha=0.25, color=theme["fg"])
    fig.tight_layout(pad=3.5)
    out = workdir / f"chart_{hashlib.sha1(json.dumps(scene).encode()).hexdigest()[:10]}.png"
    fig.savefig(out, facecolor=theme["bg"])
    plt.close(fig)
    # Fit inside the frame with a 6% margin on all sides
    margin = 0.06
    img = ImageClip(str(out))
    scale = min(W * (1 - 2 * margin) / img.w, H * (1 - 2 * margin) / img.h)
    img = img.resized(scale)
    x = (W - img.w) // 2
    y = (H - img.h) // 2
    img = img.with_position((x, y))
    return CompositeVideoClip([img], size=(W, H))


def build_outro(scene, theme):
    bg = ColorClip(size=(W, H), color=rgb(theme["bg"]))
    txt = centered_text_clip(scene["text"], theme, theme["title_font_size"],
                             color=theme["accent"], bold=True)
    return CompositeVideoClip([bg, txt], size=(W, H))


BRAND_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")


def load_branding(cfg: dict | None, spec_dir: Path):
    """Pre-load the optional `branding` logo as (clip, scenes); None if absent."""
    if not cfg:
        return None
    logo = Path(cfg["logo"])
    if not logo.is_absolute():
        logo = spec_dir / logo
    if not logo.is_file():
        print(f"[warn] branding logo not found: {logo}; rendering without logo")
        return None
    pos = cfg.get("position", "bottom-right")
    if pos not in BRAND_POSITIONS:
        raise ValueError(f"branding.position must be one of {BRAND_POSITIONS}, got {pos!r}")
    margin = max(1, int(cfg.get("margin", 48)))  # never touch the frame border
    clip = ImageClip(str(logo))  # PNG alpha channel becomes the clip's mask
    max_h = int(H * cfg.get("max_height", 0.10))
    if clip.h != max_h:
        clip = clip.resized(height=max_h)
    clip = clip.with_opacity(cfg.get("opacity", 1.0))  # multiplies the alpha mask
    x = margin if pos.endswith("left") else W - clip.w - margin
    y = margin if pos.startswith("top") else H - clip.h - margin
    return clip.with_position((x, y)), cfg.get("scenes", "all")


def apply_branding(clip, stype: str, brand):
    """Overlay the logo if this scene type is selected by `scenes`."""
    if brand is None or (brand[1] != "all" and stype not in brand[1]):
        return clip
    # Pin the scene at "center" exactly as concatenate_videoclips(method=
    # "compose") does, so animated positions (slide-up) are not shifted.
    return CompositeVideoClip([clip.with_position("center"),
                               brand[0].with_duration(clip.duration)],
                              size=(W, H)).with_duration(clip.duration)


BUILDERS = {
    "title": build_title,
    "bullets": build_bullets,
    "image": build_image,
    "chart": build_chart,
    "outro": build_outro,
}


def apply_anim(clip, anim: str, duration: float):
    if anim in ("fade", "kenburns"):
        return clip.with_effects([vfx.FadeIn(ANIM_IN), vfx.FadeOut(ANIM_IN)])
    if anim == "slide-up":
        def pos(t):
            return ("center", H / 2 + min(t / ANIM_IN, 1) * SLIDE_DIST)
        return clip.with_position(pos).with_effects([vfx.FadeIn(ANIM_IN)])
    if anim == "zoom-in":
        return clip.resized(lambda t: 1.0 + min(t / (duration * 0.8), 1) * 0.05) \
                   .with_effects([vfx.FadeIn(ANIM_IN)])
    return clip


def _ffmpeg_exe() -> str:
    from shutil import which
    p = which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _export_pptx(spec, scene_artifacts, output):
    try:
        import pptx_export
        pptx_path = str(Path(output).with_suffix(".pptx"))
        pptx_export.build_pptx(spec, scene_artifacts, pptx_path)
        print(f"[done] {pptx_path} ({len(scene_artifacts)} slides)")
    except Exception as e:
        print(f"[warn] pptx export failed: {e}")


def render(spec_path: str, output: str, workdir: str | None = None):
    spec = load_spec(spec_path)
    theme = spec["theme"]
    voice_cfg = spec.get("voice", {})
    workdir = Path(workdir or Path(spec_path).parent / ".render_cache")
    workdir.mkdir(parents=True, exist_ok=True)
    brand = load_branding(spec.get("branding"), Path(spec_path).resolve().parent)

    video_clips, audios = [], []
    scene_artifacts = []
    t_cursor = 0.0
    fps = spec["meta"]["fps"]
    stream = len(spec["timeline"]) > 12
    seg_dir = workdir / "segments"
    if stream:
        seg_dir.mkdir(exist_ok=True)
        print(f"[stream] long timeline ({len(spec['timeline'])} scenes); rendering per-scene segments")
    for i, scene in enumerate(spec["timeline"]):
        stype = scene["type"]
        builder = BUILDERS[stype]
        if stype in ("image", "chart", "title", "bullets"):
            clip = builder(scene, theme, workdir)
        else:
            clip = builder(scene, theme)

        try:
            audio_path, audio_dur = synth_narration(scene, workdir, voice_cfg)
        except RuntimeError as e:
            if "MISTRAL_API_KEY" in str(e):
                print(f"[warn] {e} — rendering scene {i} without narration")
                audio_path, audio_dur = None, 0.0
            else:
                raise

        dur = scene_duration(scene, audio_dur)
        clip = clip.with_duration(dur)
        clip = apply_anim(clip, scene.get("anim", "fade"), dur)
        # Animations like zoom-in resize frames over time even though the
        # clip's nominal size stays 1920x1080; composite onto the fixed canvas
        # so encoding always sees constant frame dimensions (variable-size
        # streams decode as garbage/static in players).
        if scene.get("anim") in ("zoom-in", "kenburns"):
            clip = CompositeVideoClip([clip.with_position("center")], size=(W, H))\
                .with_duration(dur)
        clip = apply_branding(clip, stype, brand)

        if audio_path:
            audios.append(AudioFileClip(audio_path).with_start(t_cursor))

        # Bound memory on long timelines: render each scene straight to a
        # disk segment and release it, instead of holding every composite.
        if stream:
            seg = seg_dir / f"seg_{i:03d}.mp4"
            clip.write_videofile(str(seg), fps, codec="libx264", logger=None)
            clip.close()
            del clip
        else:
            video_clips.append(clip)

        art = {}
        if audio_path:
            art["narration_path"] = audio_path
        img_path = scene_image_clip(scene, theme, workdir) if scene.get("image") else None
        if stype == "chart":
            art["chart_png"] = str(workdir / f"chart_{hashlib.sha1(json.dumps(scene).encode()).hexdigest()[:10]}.png")
        if img_path:
            if scene.get("image", {}).get("placement", "background") in ("left", "right"):
                art["image_png"] = img_path
            else:
                art["bg_image_png"] = img_path
                scrim, _ = imggen.scrim_for_contrast(
                    img_path, theme["fg"], theme["bg"], img_opacity=scene["image"].get("opacity", 0.25))
                art["scrim"] = scrim
        scene_artifacts.append(art)

        print(f"[scene {i}] {stype}: {dur:.2f}s starting at {t_cursor:.2f}s"
              + (f" (narration {audio_dur:.2f}s)" if audio_path else ""))
        t_cursor += dur

    if stream:
        # Concatenate segments with the ffmpeg concat demuxer (O(1) memory;
        # MoviePy re-reading 31 segment files OOMs). Identical codecs/params
        # make stream copy safe.
        seg_files = sorted(seg_dir.glob("seg_*.mp4"))
        concat_list = seg_dir / "concat.txt"
        concat_list.write_text("".join("file '" + str(p.resolve()) + "'\n" for p in seg_files))
        ffmpeg = _ffmpeg_exe()
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", str(concat_list), "-c", "copy", output]
        subprocess.run(cmd, check=True)
        # Mux narration: one pass with all audio offsets as inputs
        if audios:
            mixed = workdir / "narration_all.wav"
            CompositeAudioClip(audios).write_audiofile(str(mixed), logger=None)
            tmp = str(Path(output).with_suffix(".mux.mp4"))
            subprocess.run([ffmpeg, "-y", "-loglevel", "error",
                           "-i", output, "-i", str(mixed),
                           "-c:v", "copy", "-c:a", "aac",
                           "-map", "0:v", "-map", "1:a",
                           "-shortest", tmp], check=True)
            Path(tmp).replace(output)
        print(f"[done] {output} ({t_cursor:.1f}s at {fps}fps, streamed)")
        _export_pptx(spec, scene_artifacts, output)
        return

    final = concatenate_videoclips(video_clips, method="compose")
    if audios:
        final = final.with_audio(CompositeAudioClip(audios))
    final.write_videofile(output, fps=fps, codec="libx264",
                          audio_codec="aac", logger=None)
    print(f"[done] {output} ({t_cursor:.1f}s at {fps}fps)")

    _export_pptx(spec, scene_artifacts, output)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("spec")
    p.add_argument("-o", "--output", default="output/video.mp4")
    p.add_argument("--workdir", default=None)
    args = p.parse_args()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    render(args.spec, args.output, args.workdir)
