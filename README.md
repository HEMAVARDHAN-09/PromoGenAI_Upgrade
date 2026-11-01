# PromoGen AI - AI-Powered Short-Form Video Script and Promotional Content Generator

Enter **any product or service** (cleaner, skincare, electronics, food, clothing, an app, a class...) and PromoGen AI
turns it into a customised multilingual promo script, an editable storyboard, and a **real downloadable MP4**.

**Stack:** vanilla HTML/CSS/JS frontend - Python FastAPI backend - SQLite - FFmpeg. No Node.js or npm.
**Theme:** ivory white, champagne gold and charcoal (website and rendered video).

```
Product details -> AI script -> editable storyboard -> approval -> scene visuals -> voice-over -> captions
                -> FFmpeg render -> preview -> download MP4
```

## Run it (Windows PowerShell)

```powershell
# one time: Python 3.13 and FFmpeg
winget install Python.Python.3.12
winget install Gyan.FFmpeg          # then CLOSE and reopen PowerShell
ffmpeg -version                     # must print a version
ffprobe -version

cd path\to\PromoGenAI_Upgrade
.\start.ps1                         # creates .venv, installs requirements, creates backend\.env, starts the server
```

Open <http://127.0.0.1:8000>, register, and use the sample products **SilkGlow Anti-Frizz Hair Serum** or
**SparkleMax Kitchen Cleaner** (marked "Sample"), or click **Promote my own product**.
If scripts are blocked: `Set-ExecutionPolicy -Scope Process Bypass` then `.\start.ps1`.

## Configuration (`backend\.env`, created from `.env.example`; restart the server after editing)

| Variable | Purpose | Without it |
|---|---|---|
| `LLM_API_KEY`, `LLM_API_URL`, `LLM_MODEL` | Real AI script writing (any OpenAI-compatible chat API) | **Demo mode**: template script, labelled "Demo mode - not written by an AI". Built-in wording exists for English and Telugu only; other languages fall back to English **and say so** |
| `TTS_API_KEY`, `TTS_API_URL`, `TTS_MODEL`, `TTS_VOICE`, `TTS_VOICES` | AI voice-over (OpenAI-compatible `/audio/speech`) | Only installed system voices are offered |
| `STT_API_KEY`, `STT_API_URL`, `STT_MODEL` | Microphone buttons (OpenAI-compatible `/audio/transcriptions`) | Mic buttons explain that speech-to-text is not configured; typing still works |
| `SECRET_KEY` | Login token signing | A private key file is generated automatically |
| `MAX_UPLOAD_MB`, `MAX_VIDEO_UPLOAD_MB`, `MAX_AUDIO_MB`, `CORS_ORIGINS`, `ACCESS_TOKEN_MINUTES` | Limits and CORS | Sensible defaults |

API keys live only in `backend\.env` (git-ignored) and are never sent to the browser.

**AI scripts (OpenAI example):**
```
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```
For another OpenAI-compatible provider also set `LLM_API_URL` (for example Groq, OpenRouter, Together or a local server).
The Settings page shows "Live AI" when a key is loaded. Always proofread non-English AI output before approving.

**Voice-over:** the Studio lists the voices that genuinely exist right now. System voices come from Windows SAPI
(Settings > Time & Language > Speech to add languages), espeak-ng or macOS `say`. Voices matching the script language are
listed first; a mismatch shows a warning. Windows rarely ships a Telugu voice, so for Telugu narration set `TTS_API_KEY`
(quality for Telugu depends on your provider) or render captions only.

**Telugu / Indic text in the video** needs a font that contains the script. Windows has Nirmala UI. Otherwise copy a
Noto Sans font into `backend\fonts\` (see the README there). If no suitable font exists the Studio **refuses to render**
rather than producing empty boxes.

## How script generation works

The script contains: campaign title, hook, introduction, benefits, scene-by-scene storyboard (visual direction,
voice-over, on-screen text, caption, duration, transition), full narration, social caption, hashtags, tagline and final CTA.
It is built only from the details you entered (name, brand, category, description, features, audience, objective, platform,
duration, language, tone, CTA, extra instructions). The LLM prompt forbids invented prices, statistics and testimonials.
You can edit every field, add or remove scenes, save, regenerate and **approve**. Rendering is blocked until approval, and
the Video Studio cannot change approved wording (it only controls timing, transitions, media, voice and format).

## Video output

9:16, 16:9 and 1:1 at 720p or 1080p, H.264 MP4 with scene visuals (your uploaded image/clip, the product image, or a
charcoal and gold card), on-screen text, burned-in captions (auto-shrunk to fit), optional logo and brand strip, fade or cut
transitions, optional voice-over. Only one render runs at a time server-wide; others wait.

## Test it

```powershell
cd backend
.\.\.venv313\Scripts\python.exe test_core.py   # offline: script logic, 3 aspect ratios, font handling (needs FFmpeg for render checks)
# in a second window while the server is running:
.\.venv313\Scripts\python.exe smoke_test.py   # full API workflow for SilkGlow and Kitchen Cleaner, incl. render + download
.\.venv313\Scripts\python.exe smoke_test.py --no-render
```
Manual check: generate a script for each sample product and confirm the wording changes; edit and approve; render 9:16;
open the preview and download the MP4; confirm `ffprobe backend\renders\<file>.mp4` shows h264.

## Feature status

| Feature | Status |
|---|---|
| Register, login, per-user data, SQLite persistence, project history | Implemented |
| Any product or service (catalog, search, custom product with image and logo) | Implemented |
| Script generation, edit, save, regenerate, approve | Implemented |
| Live AI script | Needs `LLM_API_KEY` |
| Demo script (template; English and Telugu wording) | Implemented, always labelled as demo |
| Real MP4 rendering (3 formats, captions, transitions, logo) | Implemented, needs FFmpeg |
| System voice-over with voice and language picker | Implemented, depends on installed voices |
| AI voice-over | Needs `TTS_API_KEY` |
| Microphone to text | Needs `STT_API_KEY` (browser mic permission required) |
| Telugu and other Indic text in video | Needs a font (Windows: built in) |

## Known limitations

- No AI image or video generation: scenes without media use the product image or a styled card.
- Captions show each scene's caption text as a block; they are not word-by-word timed and there is no SRT export.
- Transitions are fade-to-black or cut (no crossfade or motion effects); no background music.
- Demo-mode wording is generic template text in only English and Telugu; real multilingual quality needs an LLM.
- Telugu pronunciation of system voices and AI voices must be checked by ear.
- Login token is kept in `localStorage` and logout is client-side. Login throttling is in memory (resets on restart).
- Renders run in-process threads; for many users run a task queue.
