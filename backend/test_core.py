r"""Offline tests for script generation and video rendering (no server, no API keys needed).

    cd backend
    .\.venv\Scripts\python.exe test_core.py

Needs FFmpeg on PATH for the rendering checks (they are reported as SKIPPED when it is missing).
"""
import json
import subprocess
import sys
from pathlib import Path

import render
import scriptgen

failures, skipped = [], []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def req(product, **kw):
    d = {"product": product, "target_audience": "busy families", "marketing_objective": "Increase sales", "platform": "Instagram Reels",
         "duration": 30, "tone": "Energetic", "language": "English", "call_to_action": "Order today", "key_selling_points": "", "instructions": ""}
    d.update(kw)
    return d


CAT = {c["id"]: c for c in json.loads((Path(__file__).parent / "catalog.json").read_text(encoding="utf-8"))}
SERUM, CLEANER = CAT["s01"], CAT["s02"]


def main():
    # ---- script generation (demo generator) -------------------------------------------------------------
    a = scriptgen.demo_script(req(SERUM, target_audience="people with frizzy hair", call_to_action="Shop SilkGlow today"))
    b = scriptgen.demo_script(req(CLEANER, target_audience="busy families", call_to_action="Order SparkleMax today"))
    for label, s, prod in (("SilkGlow", a, SERUM), ("Kitchen Cleaner", b, CLEANER)):
        check(f"{label}: has every script part", all(s.get(k) for k in ("campaign_title", "hook", "introduction", "benefits", "complete_script",
              "scenes", "caption", "hashtags", "tagline", "final_cta")))
        check(f"{label}: scene durations add up to 30", sum(x["duration"] for x in s["scenes"]) == 30, [x["duration"] for x in s["scenes"]])
        check(f"{label}: every scene has voiceover, on-screen text, caption, visual, duration",
              all(x["voiceover"] and x["onscreen_text"] and x["caption"] and x["visual"] and x["duration"] >= 1 for x in s["scenes"]))
        check(f"{label}: uses its own product name and features", prod["name"].lower() in s["complete_script"].lower() and prod["features"][0].lower() in s["complete_script"].lower())
        check(f"{label}: marked as demo", s["mode"] == "demo")
    check("different products give different scripts", a["complete_script"] != b["complete_script"] and a["hook"] != b["hook"])
    check("SilkGlow does not mention kitchen / Kitchen Cleaner does not mention hair",
          "kitchen" not in a["complete_script"].lower() and "hair" not in b["complete_script"].lower())
    check("category profiles detected", a["product_category_profile"] == "beauty" and b["product_category_profile"] == "home")
    check("electronics detected", scriptgen.profile_for({"category": "Electronics", "name": "Earbuds"})[0] == "electronics")
    check("unknown product still works", len(scriptgen.demo_script(req({"name": "Zorb", "category": "Misc", "description": "", "features": []}))["scenes"]) >= 3)
    for dur in (15, 30, 45, 60):
        check(f"duration {dur}s adds up", sum(x["duration"] for x in scriptgen.demo_script(req(CLEANER, duration=dur))["scenes"]) == dur)

    te = scriptgen.demo_script(req(CLEANER, language="Telugu", call_to_action="ఈరోజే ఆర్డర్ చేయండి"))
    check("Telugu demo script is in Telugu", any("\u0c00" <= ch <= "\u0c7f" for ch in te["hook"] + te["scenes"][1]["voiceover"]) and not te["notice"])
    check("Telugu script keeps the product name", CLEANER["name"] in te["complete_script"])
    hi = scriptgen.demo_script(req(CLEANER, language="Hindi"))
    check("Hindi demo falls back to English and SAYS so", hi["notice"] and "Hindi" in hi["notice"] and hi["language"] == "English")

    # ---- LLM output normalisation ----------------------------------------------------------------------
    raw = {"campaign_title": "T", "hook": "H", "scenes": [{"title": "A", "voiceover": "v1", "onscreen_text": "x", "duration": 5},
                                                          {"title": "B", "voiceover": "v2"}], "hashtags": "one two #three", "benefits": "a; b"}
    n = scriptgen.normalize_script(raw, req(CLEANER, duration=20), "live")
    check("normalise: durations scaled to request", sum(x["duration"] for x in n["scenes"]) == 20)
    check("normalise: hashtags/benefits/caption repaired", n["hashtags"][0] == "#one" and n["benefits"] == ["a", "b"] and n["scenes"][0]["caption"] == "v1")
    check("normalise: mode kept as 'live'", n["mode"] == "live")
    for bad in (None, [], {"scenes": []}, {"scenes": "x"}):
        try:
            scriptgen.normalize_script(bad, req(CLEANER), "live"); ok = False
        except ValueError:
            ok = True
        check(f"normalise rejects {bad!r}", ok)
    p = scriptgen.build_prompt(req(CLEANER, language="Telugu"))
    check("LLM prompt carries product, language and honesty rules", "SparkleMax" in p and "Telugu" in p and "Do not invent" in p)

    # ---- rendering (needs FFmpeg) ---------------------------------------------------------------------
    if not render.ffmpeg_available():
        skipped.append("rendering (FFmpeg not found)"); print("SKIP  rendering checks - install FFmpeg")
    else:
        for asp, size in (("9:16", (720, 1280)), ("16:9", (1280, 720)), ("1:1", (720, 720))):
            f = render.render_video(990, b["scenes"][:3], asp, "720p", captions=True, brand_text="SparkleMax")
            out = render.RENDERS / f
            pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                                            "-of", "json", str(out)], capture_output=True, text=True).stdout)["streams"][0]
            check(f"{asp} MP4 is real H.264 at {size[0]}x{size[1]}", out.read_bytes()[4:8] == b"ftyp" and pr["codec_name"] == "h264" and (pr["width"], pr["height"]) == size, pr)
            out.unlink(missing_ok=True)
        sup = render.script_support()
        print("      fonts available for:", [k for k, v in sup.items() if v] or "Latin only")
        if sup["telugu"]:
            f = render.render_video(991, te["scenes"][:2], "9:16", "720p")
            check("Telugu video renders (Telugu font present)", (render.RENDERS / f).is_file()); (render.RENDERS / f).unlink(missing_ok=True)
        else:
            try:
                render.render_video(991, te["scenes"][:2], "9:16", "720p"); ok = False
            except render.RenderError as e:
                ok = "backend/fonts" in str(e)
            check("Telugu WITHOUT a font is refused with instructions (no empty boxes)", ok)
        v = render.available_voices()
        check("voice list always offers 'No voiceover'", v[0]["id"] == "none")
        print("      voices on this machine:", [x["id"] for x in v])

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: " + "; ".join(failures)) + (f"   (skipped: {', '.join(skipped)})" if skipped else ""))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
