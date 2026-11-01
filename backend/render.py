"""PromoGen AI video renderer.

Builds a real MP4 with FFmpeg from an approved storyboard:
  * per-scene background: uploaded image, uploaded video clip, product image, or a charcoal/champagne card
  * on-screen text, optional burned-in captions, optional logo, brand strip
  * fade transitions (or hard cuts)
  * optional voiceover (system voice chosen from the voices installed on this machine, or an external TTS API)

FFmpeg / FFprobe must be on PATH.  Indic/Arabic text needs a font that really contains those glyphs: the renderer
looks in backend/fonts/ first, then in the operating system, and REFUSES to render (with a clear message)
rather than drawing empty boxes.
"""
import os
import re
import shutil
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

BASE = Path(__file__).resolve().parent
RENDERS = BASE / "renders"
TEMP = BASE / "render_tmp"
FONT_DIR = BASE / "fonts"
RENDERS.mkdir(exist_ok=True)
TEMP.mkdir(exist_ok=True)
FONT_DIR.mkdir(exist_ok=True)

FPS = 30
FADE = 0.4
# Ivory / champagne gold / charcoal palette
IVORY = (250, 246, 238)
CHAMPAGNE = (201, 169, 110)
CHARCOAL = (28, 28, 32)
CHARCOAL_SOFT = (52, 47, 42)
MUTED = (190, 182, 168)


class RenderError(Exception):
    """A readable rendering failure that is safe to show to the user."""


# --------------------------------------------------------------------------- tools
def ffmpeg_available():
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _run(cmd, what):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RenderError("FFmpeg was not found on PATH. Install FFmpeg (winget install Gyan.FFmpeg), "
                          "restart PowerShell and run `ffmpeg -version` to confirm.")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-6:]
        raise RenderError(f"FFmpeg failed while {what}: " + " | ".join(tail))
    return r


def media_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def has_video_stream(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    return r.returncode == 0 and "video" in r.stdout


# --------------------------------------------------------------------------- fonts (script aware)
SCRIPT_RANGES = {"telugu": (0x0C00, 0x0C7F), "kannada": (0x0C80, 0x0CFF), "tamil": (0x0B80, 0x0BFF),
                 "malayalam": (0x0D00, 0x0D7F), "bengali": (0x0980, 0x09FF), "devanagari": (0x0900, 0x097F),
                 "arabic": (0x0600, 0x06FF)}
SCRIPT_SAMPLE = {"telugu": "క", "kannada": "ಕ", "tamil": "க", "malayalam": "ക", "bengali": "ক", "devanagari": "क", "arabic": "ا"}
NIRMALA = ["C:/Windows/Fonts/NirmalaB.ttf", "C:/Windows/Fonts/Nirmala.ttf"]
SYSTEM_FONTS = {
    "arabic": ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arial.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}
LATIN_FONTS = [
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_GLOB_ROOTS = ["/usr/share/fonts", "/usr/local/share/fonts", "/Library/Fonts", "/System/Library/Fonts", str(Path.home() / ".fonts"),
                   "C:/Windows/Fonts"]


def _char_script(ch):
    o = ord(ch)
    for name, (lo, hi) in SCRIPT_RANGES.items():
        if lo <= o <= hi:
            return name
    return None


def scripts_in(text):
    return {s for s in (_char_script(c) for c in str(text or "")) if s}


@lru_cache(maxsize=64)
def _load(path, size):
    return ImageFont.truetype(path, size)


def _glyph_bytes(font, ch):
    im = Image.new("L", (96, 96), 0)
    ImageDraw.Draw(im).text((8, 8), ch, font=font, fill=255)
    return im.tobytes()


def _covers(path, sample):
    """True when the font draws `sample` as a real glyph (not blank and not the font's missing-glyph box)."""
    try:
        f = ImageFont.truetype(str(path), 48)
        a, b = _glyph_bytes(f, sample), _glyph_bytes(f, "\U0010FFFF")
        return a != b and any(a)
    except Exception:  # noqa: BLE001 - unreadable/unsupported font file
        return False


def _font_files(folder):
    try:
        return [p for p in Path(folder).rglob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc")]
    except OSError:
        return []


_script_font_cache = {}


def script_font_path(script):
    """Path of a font that really contains `script` glyphs, or None. Cached."""
    if script in _script_font_cache:
        return _script_font_cache[script]
    sample = SCRIPT_SAMPLE[script]
    cands = _font_files(FONT_DIR)  # anything the user dropped into backend/fonts that covers the script wins
    cands += [Path(p) for p in NIRMALA + SYSTEM_FONTS.get(script, []) if Path(p).exists()]
    for root in FONT_GLOB_ROOTS:
        cands += [p for p in _font_files(root) if script in p.name.lower()]
    cands.sort(key=lambda p: (0 if "bold" in p.name.lower() or p.name.lower().endswith("b.ttf") else 1))
    found = next((str(p) for p in cands if _covers(p, sample)), None)
    _script_font_cache[script] = found
    return found


def script_support():
    """{script: bool} - which non-Latin scripts can be drawn on this machine (for the UI)."""
    return {s: bool(script_font_path(s)) for s in SCRIPT_RANGES}


def _require_font(script):
    path = script_font_path(script)
    if not path:
        raise RenderError(
            f"No {script.title()} font was found on this computer, so the video text would show as empty boxes. "
            f"Download 'Noto Sans {script.title()}' (free, fonts.google.com/noto), copy the .ttf file into "
            f"backend/fonts/, restart the server and render again. (Windows normally already has Nirmala UI.)")
    return path


@lru_cache(maxsize=1)
def _latin_path():
    for p in LATIN_FONTS:
        if Path(p).exists():
            return p
    for p in _font_files(FONT_DIR):
        if _covers(p, "A"):
            return str(p)
    return None


def _latin(size):
    p = _latin_path()
    if p:
        return _load(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _runs(text, size):
    """Split text into (substring, font) runs so each character is drawn with a font that has it."""
    runs, cur_cls, buf = [], None, ""
    for ch in str(text):
        sc = _char_script(ch)
        cls = sc if sc else ("latin" if not ch.isspace() else cur_cls)
        cls = cls or "latin"
        if cur_cls is not None and cls != cur_cls:
            runs.append((buf, cur_cls)); buf = ""
        cur_cls = cls; buf += ch
    if buf:
        runs.append((buf, cur_cls))
    return [(t, _latin(size) if c == "latin" else _load(_require_font(c), size)) for t, c in runs]


def _tw(draw, text, size):
    return sum(draw.textlength(t, font=f) for t, f in _runs(text, size))


def _draw(draw, xy, text, size, fill):
    x, y = xy
    base = y + int(size * 0.92)
    for t, f in _runs(text, size):
        draw.text((x, base), t, font=f, fill=fill, anchor="ls")
        x += draw.textlength(t, font=f)


def _wrap(draw, text, size, max_width, max_lines):
    lines, cur = [], ""
    for word in str(text).split():
        trial = (cur + " " + word).strip()
        if _tw(draw, trial, size) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur); cur = word
    if cur:
        lines.append(cur)
    clipped = len(lines) > max_lines
    return lines[:max_lines], clipped


def _fit(draw, text, size, min_size, max_width, max_lines):
    """Shrink the font until the text fits in max_lines; only clip with an ellipsis as a last resort."""
    while True:
        lines, clipped = _wrap(draw, text, size, max_width, max_lines)
        too_wide = any(_tw(draw, ln, size) > max_width for ln in lines)
        if (not clipped and not too_wide) or size <= min_size:
            if clipped:
                lines[-1] = lines[-1].rstrip(" .,;") + "…"
            return lines, size
        size = max(min_size, int(size * 0.92))


# --------------------------------------------------------------------------- voiceover
def _system_tts_command():
    if os.name == "nt" and shutil.which("powershell"):
        return "sapi"
    if shutil.which("espeak-ng"):
        return "espeak-ng"
    if shutil.which("espeak"):
        return "espeak"
    if shutil.which("say"):
        return "say"
    return None


_voice_cache = {}


def list_system_voices():
    """Installed offline voices: [{'id': 'system:<name>', 'label': ..., 'lang': 'te-IN'}]. Empty when none can be listed."""
    engine = _system_tts_command()
    if not engine:
        return []
    if engine in _voice_cache:
        return _voice_cache[engine]
    out = []
    try:
        if engine == "sapi":
            ps = ("Add-Type -AssemblyName System.Speech;"
                  "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
                  "ForEach-Object { $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture.Name }")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=25)
            for line in r.stdout.splitlines():
                if "|" in line:
                    name, lang = [x.strip() for x in line.split("|", 1)]
                    out.append({"id": f"system:{name}", "label": f"{name} ({lang})", "lang": lang})
        elif engine in ("espeak-ng", "espeak"):
            r = subprocess.run([engine, "--voices"], capture_output=True, text=True, timeout=15)
            seen = set()
            for line in r.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[1] not in seen:
                    seen.add(parts[1])
                    out.append({"id": f"system:{parts[1]}", "label": f"{parts[3].replace('_', ' ')} ({parts[1]})", "lang": parts[1]})
        elif engine == "say":
            r = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=15)
            for line in r.stdout.splitlines():
                m = re.match(r"^(.+?)\s{2,}([a-z]{2}[_-][A-Za-z]{2,})\s", line)
                if m:
                    out.append({"id": f"system:{m.group(1).strip()}", "label": f"{m.group(1).strip()} ({m.group(2)})",
                                "lang": m.group(2).replace("_", "-")})
    except (OSError, subprocess.SubprocessError):
        out = []
    _voice_cache[engine] = out
    return out


def api_voices():
    if not os.getenv("TTS_API_KEY"):
        return []
    names = [v.strip() for v in os.getenv("TTS_VOICES", os.getenv("TTS_VOICE", "alloy")).split(",") if v.strip()]
    return [{"id": f"api:{n}", "label": f"AI voice - {n} (multilingual; quality depends on provider)", "lang": "multi"} for n in names]


def available_voices():
    """Voice options that will genuinely work right now. The UI only offers what is listed here."""
    voices = [{"id": "none", "label": "No voiceover", "lang": ""}]
    sysv = list_system_voices()
    if _system_tts_command():
        voices.append({"id": "system", "label": "System default voice", "lang": "default"})
    voices += sysv + api_voices()
    return voices


def voice_ok(voice_id):
    return voice_id in {v["id"] for v in available_voices()} or (voice_id == "api" and bool(os.getenv("TTS_API_KEY")))


def synthesize(text, out_path, voice):
    """Write speech for `text` to an audio file. `voice` is 'system', 'system:<name>', 'api' or 'api:<voice>'."""
    text = (text or "").strip()
    if not text:
        return None
    provider, _, chosen = voice.partition(":")
    if provider == "api":
        import httpx
        key = os.getenv("TTS_API_KEY")
        if not key:
            raise RenderError("TTS_API_KEY is not configured on the server.")
        url = os.getenv("TTS_API_URL", "https://api.openai.com/v1/audio/speech")
        payload = {"model": os.getenv("TTS_MODEL", "tts-1"), "voice": chosen or os.getenv("TTS_VOICE", "alloy"),
                   "input": text[:4000], "response_format": "mp3"}
        try:
            r = httpx.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=90)
        except httpx.HTTPError as e:
            raise RenderError(f"Could not reach the TTS provider: {e}")
        if r.status_code >= 400:
            raise RenderError(f"TTS provider error {r.status_code}: {r.text[:200]}")
        out_path = out_path.with_suffix(".mp3")
        out_path.write_bytes(r.content)
        return out_path

    engine = _system_tts_command()
    if engine == "sapi":
        script = ("Add-Type -AssemblyName System.Speech;"
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  "if($env:PG_VOICE){$s.SelectVoice($env:PG_VOICE)};"
                  "$s.SetOutputToWaveFile($env:PG_OUT);$s.Speak($env:PG_TEXT);$s.Dispose()")
        env = {**os.environ, "PG_OUT": str(out_path), "PG_TEXT": text, "PG_VOICE": chosen}
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, env=env)
    elif engine in ("espeak-ng", "espeak"):
        cmd = [engine] + (["-v", chosen] if chosen else []) + ["-w", str(out_path), text]
        r = subprocess.run(cmd, capture_output=True, text=True)
    elif engine == "say":
        out_path = out_path.with_suffix(".aiff")
        r = subprocess.run(["say"] + (["-v", chosen] if chosen else []) + ["-o", str(out_path), text], capture_output=True, text=True)
    else:
        raise RenderError("No offline text-to-speech engine is available on this machine.")
    if r.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 100:
        raise RenderError("The system voice failed to produce audio: " + (r.stderr or "").strip()[:200])
    return out_path


# --------------------------------------------------------------------------- drawing
def _size(aspect, resolution):
    w, h = {"720p": (720, 1280), "1080p": (1080, 1920)}.get(resolution, (720, 1280))
    if aspect == "16:9":
        return h, w
    if aspect == "1:1":
        return w, w
    return w, h


def _gradient(w, h):
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        px.line((0, y, w, y), fill=tuple(int(a + (b - a) * t) for a, b in zip(CHARCOAL, CHARCOAL_SOFT)))
    return img


def _logo_image(path, w, h):
    try:
        with Image.open(path) as im:
            logo = ImageOps.exif_transpose(im).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None
    logo.thumbnail((int(w * 0.22), int(h * 0.09)), Image.LANCZOS)
    return logo


def _compose_overlay(w, h, scene, idx, total, captions, transparent, brand, logo):
    """Text layer. Returns an RGBA image (transparent=True) or an RGB card with gradient behind it."""
    base = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if transparent else _gradient(w, h).convert("RGBA")
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    landscape = w > h
    pad = int(w * 0.07)
    short = min(w, h)

    if transparent:  # legibility scrims over photos / clips
        for y in range(h):
            t = y / max(h - 1, 1)
            a = int(170 * max(0, (t - 0.4) / 0.6) ** 1.2 + 90 * max(0, (0.3 - t) / 0.3))
            if a:
                d.line((0, y, w, y), fill=CHARCOAL + (min(a, 220),))
    d.rounded_rectangle((int(w * .035), int(h * .03), int(w * .965), int(h * .97)), radius=int(short * .04),
                        outline=CHAMPAGNE + (255,), width=max(2, int(short * .005)))

    title = str(scene.get("onscreen_text") or scene.get("title") or f"Scene {idx + 1}")
    cap = str(scene.get("caption") or scene.get("voiceover") or "") if captions else ""
    label = str(scene.get("title") or "").upper()[:40]

    tsize = int(short * (0.075 if not landscape else 0.085))
    lines, tsize = _fit(d, title, tsize, int(tsize * 0.55), w - 2 * pad, 4)
    th = int(tsize * 1.3)
    y0 = int(h * (0.18 if not landscape else 0.2))
    if label:
        lsz = max(16, int(short * 0.028))
        _draw(d, (pad, y0 - int(lsz * 2.6)), label, lsz, CHAMPAGNE + (255,))
        d.line((pad, y0 - int(lsz * 0.9), pad + int(short * 0.12), y0 - int(lsz * 0.9)), fill=CHAMPAGNE + (255,), width=max(2, int(short * .004)))
    for i, ln in enumerate(lines):
        _draw(d, (pad + 2, y0 + i * th + 2), ln, tsize, (0, 0, 0, 150))
        _draw(d, (pad, y0 + i * th), ln, tsize, IVORY + (255,))

    if cap:
        csize = max(18, int(short * 0.036))
        clines, csize = _fit(d, cap, csize, max(15, int(csize * 0.7)), w - 2 * pad, 4)
        ch = int(csize * 1.35)
        box_h = ch * len(clines) + int(short * 0.04)
        by = int(h * (0.66 if not landscape else 0.70))
        d.rounded_rectangle((pad - 14, by - 10, w - pad + 14, by - 10 + box_h), radius=18, fill=CHARCOAL + (205,),
                            outline=CHAMPAGNE + (120,), width=1)
        for i, ln in enumerate(clines):
            _draw(d, (pad, by + i * ch), ln, csize, IVORY + (255,))

    bsz = max(14, int(short * 0.024))
    _draw(d, (pad, int(h * 0.915)), brand, bsz, CHAMPAGNE + (255,))
    counter = f"{idx + 1}/{total}"
    _draw(d, (w - pad - _tw(d, counter, bsz), int(h * 0.915)), counter, bsz, MUTED + (255,))
    if logo is not None:
        layer.paste(logo, (w - pad - logo.width, int(h * 0.05)), logo)
    return Image.alpha_composite(base, layer)


# --------------------------------------------------------------------------- main
def check_fonts(scenes, captions=True):
    """Fail early (before any slow work) if some text needs a font this machine does not have."""
    need = set()
    for s in scenes:
        for k in ("title", "onscreen_text", "voiceover", "caption"):
            need |= scripts_in(s.get(k))
    for sc in need:
        _require_font(sc)


def render_video(project_id, scenes, aspect="9:16", resolution="720p", voice="none", captions=True, progress=None,
                 logo_path=None, brand_text=""):
    """Render scenes to an MP4 in backend/renders and return the file name.

    Each scene is a dict with: duration, onscreen_text, title, voiceover, caption, transition,
    and optionally `media_path` (absolute Path) + `media_type` ("image" | "video").
    """
    if not scenes:
        raise RenderError("At least one scene is required.")
    if not ffmpeg_available():
        raise RenderError("FFmpeg/FFprobe not found on PATH. Install FFmpeg and restart your terminal.")
    check_fonts(scenes, captions)
    brand = (str(brand_text).strip() or "PROMOGEN AI").upper()[:32]

    job = uuid.uuid4().hex
    work = TEMP / job
    work.mkdir(parents=True)
    w, h = _size(aspect, resolution)
    logo = _logo_image(logo_path, w, h) if logo_path else None
    total = len(scenes)
    clips, any_audio = [], voice != "none"
    try:
        for idx, scene in enumerate(scenes):
            if progress:
                progress(f"Rendering scene {idx + 1} of {total}")
            try:
                dur = float(scene.get("duration", 3))
            except (TypeError, ValueError):
                dur = 3.0
            dur = max(1.0, min(dur, 30.0))

            audio = None
            if any_audio and str(scene.get("voiceover") or "").strip():
                if progress:
                    progress(f"Creating voiceover for scene {idx + 1} of {total}")
                audio = synthesize(scene["voiceover"], work / f"voice_{idx:03d}.wav", voice)
                dur = max(dur, min(media_duration(audio) + 0.4, 30.0))

            media, mtype = scene.get("media_path"), scene.get("media_type")
            fade = str(scene.get("transition", "fade")).lower() != "cut"
            vf_fade = (f",fade=t=in:st=0:d={FADE},fade=t=out:st={max(dur - FADE, 0):.2f}:d={FADE}" if fade else "")
            clip = work / f"clip_{idx:03d}.mp4"
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

            if media and mtype == "video":
                overlay = work / f"overlay_{idx:03d}.png"
                _compose_overlay(w, h, scene, idx, total, captions, True, brand, logo).save(overlay)
                cmd += ["-stream_loop", "-1", "-i", str(media), "-loop", "1", "-framerate", str(FPS), "-i", str(overlay)]
                vgraph = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={FPS}[bg];"
                          f"[bg][1:v]overlay=format=auto,format=yuv420p{vf_fade}[v]")
                nxt = 2
            else:
                frame = work / f"frame_{idx:03d}.png"
                if media and mtype == "image":
                    with Image.open(media) as src:
                        bg = ImageOps.fit(ImageOps.exif_transpose(src).convert("RGB"), (w, h), Image.LANCZOS).convert("RGBA")
                    out = Image.alpha_composite(bg, _compose_overlay(w, h, scene, idx, total, captions, True, brand, logo))
                else:
                    out = _compose_overlay(w, h, scene, idx, total, captions, False, brand, logo)
                out.convert("RGB").save(frame)
                cmd += ["-loop", "1", "-framerate", str(FPS), "-i", str(frame)]
                vgraph = f"[0:v]format=yuv420p{vf_fade}[v]"
                nxt = 1

            if any_audio:
                if audio:
                    cmd += ["-i", str(audio)]
                    agraph = f";[{nxt}:a]aresample=44100,aformat=channel_layouts=stereo,apad[a]"
                else:
                    cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
                    agraph = f";[{nxt}:a]anull[a]"
                cmd += ["-filter_complex", vgraph + agraph, "-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
            else:
                cmd += ["-filter_complex", vgraph, "-map", "[v]", "-an"]
            cmd += ["-t", f"{dur:.2f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                    "-pix_fmt", "yuv420p", str(clip)]
            _run(cmd, f"rendering scene {idx + 1}")
            clips.append(clip)

        if progress:
            progress("Joining scenes into the final MP4")
        concat = work / "concat.txt"
        concat.write_text("\n".join(f"file '{p.as_posix()}'" for p in clips), encoding="utf-8")
        output = RENDERS / f"promo_{project_id}_{job[:8]}.mp4"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
               "-c", "copy", "-movflags", "+faststart", str(output)]
        _run(cmd, "joining scenes")
        if not output.exists() or output.stat().st_size < 1000:
            raise RenderError("FFmpeg finished but did not produce a valid MP4.")
        return output.name
    finally:
        shutil.rmtree(work, ignore_errors=True)
