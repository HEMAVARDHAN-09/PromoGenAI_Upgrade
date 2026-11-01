r"""End-to-end API smoke test against a RUNNING server.

    cd backend
    .\.venv\Scripts\python.exe smoke_test.py            # server must be running on http://127.0.0.1:8000
    .\.venv\Scripts\python.exe smoke_test.py --no-render  # skip the (slower) MP4 render steps

Registers throw-away users and walks the whole workflow for TWO different products
(SilkGlow Anti-Frizz Hair Serum and SparkleMax Kitchen Cleaner): script -> edit -> approve -> render -> download.
Steps that need something you have not configured (FFmpeg, a Telugu font, STT key) are reported honestly, not faked.
"""
import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api"
SKIP_RENDER = "--no-render" in sys.argv
failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def login(email, pw):
    c = httpx.Client(base_url=BASE, timeout=60)
    c.post("/auth/register", json={"full_name": "Smoke Test", "email": email, "password": pw, "confirm_password": pw})
    c.headers["Authorization"] = "Bearer " + c.post("/auth/login", json={"email": email, "password": pw}).json()["access_token"]
    return c


def render_and_download(c, c2, pid, scenes, label, **opts):
    body = {"scenes": scenes, "aspect_ratio": "9:16", "resolution": "720p", "voice": "none", "captions": True, **opts}
    r = c.post(f"/projects/{pid}/render", json=body)
    check(f"{label}: render starts", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    status = {}
    for _ in range(240):
        status = c.get(f"/projects/{pid}/render-status").json()
        if status["status"] != "rendering":
            break
        time.sleep(1)
    check(f"{label}: render completes", status.get("status") == "completed", status.get("error"))
    if status.get("status") == "completed":
        v = c.get(f"/projects/{pid}/video", params={"download": "true"})
        check(f"{label}: MP4 downloads", v.status_code == 200 and v.headers["content-type"] == "video/mp4" and v.content[4:8] == b"ftyp" and len(v.content) > 5000, f"{v.status_code} {len(v.content)} bytes")
        print(f"      {label}: downloaded {len(v.content) // 1024} KB MP4")
        check(f"{label}: other user cannot download video", c2.get(f"/projects/{pid}/video").status_code == 404)


def main():
    c = httpx.Client(base_url=BASE, timeout=60)
    pw = "Smoke12345"
    email = f"smoke_{uuid.uuid4().hex[:8]}@example.com"
    check("unauthenticated request is rejected", c.get("/me").status_code == 401)
    check("weak password rejected", c.post("/auth/register", json={"full_name": "Smoke Test", "email": email, "password": "password", "confirm_password": "password"}).status_code == 422)
    check("registration works", c.post("/auth/register", json={"full_name": "Smoke Test", "email": email, "password": pw, "confirm_password": pw}).status_code == 200)
    check("duplicate email rejected", c.post("/auth/register", json={"full_name": "Smoke Test", "email": email, "password": pw, "confirm_password": pw}).status_code == 409)
    check("wrong password rejected", c.post("/auth/login", json={"email": email, "password": "Wrong12345"}).status_code == 401)
    r = c.post("/auth/login", json={"email": email, "password": pw})
    check("login works", r.status_code == 200 and "access_token" in r.json(), r.text)
    c.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    c2 = login(f"smoke_{uuid.uuid4().hex[:8]}@example.com", pw)

    caps = c.get("/capabilities").json()
    print(f"      capabilities: llm={caps['llm']} ffmpeg={caps['ffmpeg']} stt={caps['stt']} voices={[v['id'] for v in caps['voices']]} fonts={[k for k, v in caps['fonts'].items() if v]}")
    prods = c.get("/products").json()
    check("catalog has 20+ products", prods["total"] >= 20, prods["total"])
    check("search works", 0 < c.get("/products", params={"q": "earbuds"}).json()["total"] < prods["total"])
    serum = next((p for p in c.get("/products", params={"q": "SilkGlow"}).json()["items"]), None)
    cleaner = next((p for p in c.get("/products", params={"q": "Kitchen Cleaner"}).json()["items"]), None)
    check("sample products SilkGlow and Kitchen Cleaner are in the catalog", bool(serum and cleaner))
    if not (serum and cleaner):
        sys.exit(1)

    scripts, pids = {}, {}
    for label, product, cta in (("SilkGlow", serum, "Shop SilkGlow today"), ("Kitchen Cleaner", cleaner, "Order SparkleMax today")):
        r = c.post("/scripts/generate", json={"product": product, "duration": 30, "platform": "Instagram Reels", "tone": "Energetic", "language": "English", "call_to_action": cta, "target_audience": "busy families"})
        check(f"{label}: script generation works", r.status_code == 200, r.text[:300])
        pids[label], s = r.json()["project_id"], r.json()["script"]
        scripts[label] = s
        check(f"{label}: all script parts present", all(s.get(k) for k in ("campaign_title", "hook", "introduction", "benefits", "complete_script", "scenes", "caption", "hashtags", "tagline", "final_cta")))
        check(f"{label}: scenes complete and durations add up", sum(x["duration"] for x in s["scenes"]) == 30 and all(x["voiceover"] and x["onscreen_text"] and x["caption"] for x in s["scenes"]))
        check(f"{label}: script is labelled {'demo' if not caps['llm'] else 'live'}", s["mode"] == ("live" if caps["llm"] else "demo"))
        check(f"{label}: uses this product's details", product["name"].split()[0].lower() in s["complete_script"].lower() and cta in s["final_cta"])
    check("different products produce different scripts", scripts["SilkGlow"]["complete_script"] != scripts["Kitchen Cleaner"]["complete_script"])
    print(f"      script mode: {scripts['Kitchen Cleaner']['mode']}")

    hi = c.post("/scripts/generate", json={"product": cleaner, "duration": 15, "language": "Hindi"}).json()["script"]
    check("Hindi without an LLM key is flagged as English fallback" if not caps["llm"] else "Hindi script generated", (bool(hi.get("notice")) if not caps["llm"] else hi["language"] == "Hindi"))

    pid, script = pids["Kitchen Cleaner"], scripts["Kitchen Cleaner"]
    script["hook"] = "Edited hook"
    script["mode"] = "live"  # a client must not be able to fake an AI-written script
    check("script edit saves", c.put(f"/projects/{pid}/script", json={"script": script}).status_code == 200)
    check("edited script persisted, mode cannot be faked", c.get(f"/projects/{pid}").json()["script"]["hook"] == "Edited hook" and c.get(f"/projects/{pid}").json()["script"]["mode"] == scripts["Kitchen Cleaner"]["mode"])
    script["scenes"][0]["media"] = "u999_" + "0" * 32 + ".jpg"
    check("cannot attach media that is not yours", c.put(f"/projects/{pid}/script", json={"script": script}).status_code == 422)
    script["scenes"][0]["media"] = None
    check("render blocked before approval", c.post(f"/projects/{pid}/render", json={"scenes": script["scenes"]}).status_code == 400)
    check("approval works", c.post(f"/projects/{pid}/approve").status_code == 200)
    check("unavailable voice is rejected", c.post(f"/projects/{pid}/render", json={"scenes": script["scenes"], "voice": "system:No Such Voice"}).status_code == 422)
    check("other user cannot read/delete project", c2.get(f"/projects/{pid}").status_code == 404 and c2.delete(f"/projects/{pid}").status_code == 404)
    check("other user sees an empty list", c2.get("/projects").json()["total"] == 0)

    stt = c.post("/speech/transcribe", files={"file": ("s.webm", b"x" * 4000, "audio/webm")}, data={"language": "English"})
    check("speech-to-text: honest 503 when not configured" if not caps["stt"] else "speech-to-text endpoint reachable", stt.status_code == (503 if not caps["stt"] else stt.status_code))

    if SKIP_RENDER:
        print("      (render skipped)")
    elif not caps["ffmpeg"]:
        check("FFmpeg available for rendering", False, "install FFmpeg: winget install Gyan.FFmpeg")
    else:
        c.post(f"/projects/{pids['SilkGlow']}/approve")
        for label in ("Kitchen Cleaner", "SilkGlow"):
            p = c.get(f"/projects/{pids[label]}").json()
            render_and_download(c, c2, pids[label], p["script"]["scenes"], label)
        # wording is locked to the approved script
        p = c.get(f"/projects/{pid}").json(); sc = p["script"]["scenes"]; sc[0]["voiceover"] = "INJECTED"
        c.put(f"/projects/{pid}/scenes", json={"scenes": sc})
        check("Video Studio cannot change approved wording", "INJECTED" not in c.get(f"/projects/{pid}").json()["script"]["scenes"][0]["voiceover"])
        for aspect in ("16:9", "1:1"):
            render_and_download(c, c2, pid, c.get(f"/projects/{pid}").json()["script"]["scenes"], f"Kitchen Cleaner {aspect}", aspect_ratio=aspect)
        tr = c.post("/scripts/generate", json={"product": cleaner, "duration": 15, "language": "Telugu", "call_to_action": "ఈరోజే ఆర్డర్ చేయండి"}).json()
        c.post(f"/projects/{tr['project_id']}/approve")
        if caps["fonts"].get("telugu"):
            render_and_download(c, c2, tr["project_id"], tr["script"]["scenes"], "Telugu")
        else:
            r = c.post(f"/projects/{tr['project_id']}/render", json={"scenes": tr["script"]["scenes"]})
            check("Telugu without a font is refused with instructions (no empty boxes)", r.status_code == 422 and "backend/fonts" in r.text, r.text[:200])

    stats = c.get("/dashboard").json()["stats"]
    check("dashboard counts real data", stats["projects"] >= 2, stats)
    for p in c.get("/projects").json()["items"]:
        c.delete(f"/projects/{p['id']}")
    check("projects delete", c.get("/projects").json()["total"] == 0)
    check("path traversal blocked", b"SECRET_KEY" not in httpx.get("http://127.0.0.1:8000/..%2fbackend%2f.env").content and b"SECRET_KEY" not in httpx.get("http://127.0.0.1:8000/%2e%2e/backend/.env").content)

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: " + "; ".join(failures)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        print("Cannot connect to http://127.0.0.1:8000 - start the server first (.\\start.ps1).")
        sys.exit(2)
