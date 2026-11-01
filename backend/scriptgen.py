"""PromoGen AI - script generation logic (pure Python, no web framework).

* demo_script()      built-in template generator used ONLY when no LLM key is configured.
                     Built-in wording exists for English and Telugu; any other language falls back to
                     English and says so in the `notice` field - it never pretends otherwise.
* build_prompt()     prompt sent to the configured LLM.
* normalize_script() validates/repairs an LLM answer so the editor and renderer always get every field.
"""
import json
import re

LANG_CODES = {"English": "en", "Telugu": "te", "Hindi": "hi", "Tamil": "ta", "Kannada": "kn", "Malayalam": "ml",
              "Marathi": "mr", "Bengali": "bn", "Spanish": "es", "French": "fr", "German": "de", "Arabic": "ar"}
DEMO_LANGS = ("English", "Telugu")

# category profile: keywords, English/Telugu hook (spoken), short on-screen hook, visual style
PROFILES = {
    "beauty": (("hair", "skin", "serum", "beauty", "cosmetic", "personal care", "shampoo", "makeup", "perfume"),
               "Your daily routine deserves a better finish.", "Upgrade your routine",
               "మీ రోజువారీ సంరక్షణకు కొత్త మెరుపు కావాలా?", "కొత్త మెరుపు",
               "soft beauty lighting, close-ups of texture and finish, smooth slow motion"),
    "home": (("clean", "kitchen", "home", "household", "detergent", "appliance", "laundry", "dish"),
             "Cleaning shouldn't have to be hard work.", "Clean, made easy",
             "శుభ్రత కష్టం కావాల్సిన అవసరం లేదు!", "శుభ్రత సులభం",
             "bright home setting, real surfaces, satisfying wipe-and-shine close-ups"),
    "food": (("food", "beverage", "snack", "drink", "tea", "coffee", "sweet", "spice", "restaurant"),
             "One taste and you'll know.", "One taste. You'll know.",
             "ఒక్క రుచితో మీకే తెలుస్తుంది!", "అద్భుతమైన రుచి",
             "macro food shots, steam, pour and crunch details, warm appetising light"),
    "electronics": (("electronic", "phone", "earbud", "watch", "laptop", "gadget", "device", "camera", "speaker"),
                    "Ready to take your tech to the next level?", "Level up your tech",
                    "మీ టెక్ అనుభవాన్ని మరో స్థాయికి తీసుకెళ్లండి!", "టెక్ కొత్త స్థాయి",
                    "sleek product rotation, macro detail shots, glowing screen and interface close-ups"),
    "fashion": (("cloth", "fashion", "wear", "jacket", "shoe", "apparel", "saree", "dress", "jewel"),
                "Give your style a fresh new look.", "Refresh your style",
                "మీ స్టైల్‌కు కొత్త లుక్ ఇవ్వండి!", "కొత్త లుక్",
                "lifestyle shots in motion, fabric texture close-ups, outfit reveals"),
    "fitness": (("fitness", "gym", "yoga", "sport", "workout", "health", "wellness"),
                "Your fitness journey starts today.", "Start today",
                "ఈరోజే మీ ఫిట్‌నెస్ ప్రయాణం మొదలుపెట్టండి!", "ఈరోజే మొదలు",
                "energetic training shots, sweat and motion, confident close-ups"),
    "education": (("educat", "course", "learn", "tutor", "school", "training", "coaching"),
                  "Now is the perfect time to learn something new.", "Learn something new",
                  "కొత్తగా నేర్చుకోవడానికి ఇదే సరైన సమయం!", "కొత్తగా నేర్చుకోండి",
                  "learners at work, screen and notebook shots, progress moments"),
    "travel": (("travel", "hotel", "resort", "tour", "trip", "holiday", "stay"),
               "Ready for your next escape?", "Your next escape",
               "మీ తదుపరి ప్రయాణానికి సిద్ధమా?", "మీ తదుపరి ప్రయాణం",
               "scenic establishing shots, relaxed moments, destination details"),
    "software": (("software", "app", "saas", "digital", "platform", "tool", "online"),
                 "Make your work simpler starting today.", "Work smarter",
                 "మీ పని సులభం చేసుకోండి!", "సులభమైన పని",
                 "clean screen recordings, interface highlights, before and after workflow"),
}
GENERIC = ("", "You need to see this.", "You need to see this",
           "మీరు ఇది తప్పక చూడాలి!", "తప్పక చూడండి",
           "clean hero shots of the product, clear lighting, close-ups of key details")

# beat order per scene count
ORDER = {3: ["hook", "intro", "offer"], 4: ["hook", "intro", "b1", "offer"], 5: ["hook", "intro", "b1", "b2", "offer"],
         6: ["hook", "intro", "b1", "b2", "offer", "close"], 7: ["hook", "intro", "b1", "b2", "b3", "offer", "close"],
         8: ["hook", "intro", "b1", "b2", "b3", "demo", "offer", "close"]}

# language -> (on-screen, voice-over) templates. {x} fields are filled from the product.
TEXT = {
    "English": {
        "intro": ("Meet {name}", "Introducing {name}{by_brand}. {desc}."),
        "benefit": ("{feature}", "{feature_c}."),
        "audience": ("Made for {audience}", "{name} is made for {audience}."),
        "demo": ("See it in action", "See {name} in action."),
        "offer": ("{cta}", "{cta}"),
        "close": ("{name}", "{tagline}"),
        "tagline": "{name}. Made for {audience}.",
        "caption": "Discover {name} - made for {audience}. {cta}",
        "title": "{name} | {platform} campaign",
        "intro_para": "{name}{by_brand} - {desc}.",
    },
    "Telugu": {
        "intro": ("{name} పరిచయం", "{name}{by_brand} ను పరిచయం చేస్తున్నాం. {desc}."),
        "benefit": ("{feature}", "{feature} - మీ కోసమే."),
        "audience": ("{audience} కోసం", "{name} - {audience} కోసం రూపొందించబడింది."),
        "demo": ("ఇది ఎలా పనిచేస్తుందో చూడండి", "{name} ఎలా పనిచేస్తుందో చూడండి."),
        "offer": ("{cta}", "{cta}"),
        "close": ("{name}", "{tagline}"),
        "tagline": "{name} - {audience} కోసం.",
        "caption": "{name} - {audience} కోసం. {cta}",
        "title": "{name} | {platform} ప్రచారం",
        "intro_para": "{name}{by_brand} - {desc}.",
    },
}
DEFAULT_DESC = {"English": "A smarter way to get more from every day", "Telugu": "మీ ప్రతిరోజూ మరింత సులభం"}


def profile_for(product):
    text = f"{product.get('category', '')} {product.get('name', '')}".lower()
    for key, prof in PROFILES.items():
        if any(k in text for k in prof[0]):
            return key, prof
    return "generic", GENERIC


def split_durations(total, n):
    base = max(1, total // n)
    out = [base] * n
    out[-1] = max(1, total - base * (n - 1))
    return out


def _features(d):
    p = d["product"]
    raw = d.get("key_selling_points") or ", ".join(p.get("features") or [])
    # one feature per line (or ';'); only fall back to commas when the text has no line breaks/semicolons
    parts = re.split(r"[\n;]", raw) if re.search(r"[\n;]", raw) else raw.split(",")
    return [f.strip() for f in parts if f.strip()][:6]


def demo_script(d):
    """d is ScriptRequest.model_dump(). Returns a complete script dict (mode='demo')."""
    p = d["product"]
    requested = d.get("language") or "English"
    lang = requested if requested in DEMO_LANGS else "English"
    notice = None
    if lang != requested:
        notice = (f"Demo mode has built-in wording only for English and Telugu, so this script is in English, "
                  f"not {requested}. Add LLM_API_KEY to backend/.env for a real {requested} script.")
    T = TEXT[lang]
    te = lang == "Telugu"
    key, prof = profile_for(p)
    name = (p.get("name") or "your product").strip()
    brand = (p.get("brand") or "").strip()
    audience = (d.get("target_audience") or "everyone").strip()
    cta = (d.get("call_to_action") or "Learn more today").strip()
    desc = (p.get("description") or "").strip().rstrip(".") or DEFAULT_DESC[lang]
    feats = _features(d)
    by_brand = (f" by {brand}" if not te else f" ({brand})") if brand else ""
    ctx = {"name": name, "audience": audience, "cta": cta, "desc": desc, "by_brand": by_brand,
           "platform": d.get("platform", "Short video"), "tagline": T["tagline"].format(name=name, audience=audience)}
    ctx["tagline"] = T["tagline"].format(**ctx)

    n = min(8, max(3, round(d["duration"] / 6)))
    beats = []
    for b in ORDER[n]:
        if b in ("b1", "b2", "b3"):
            i = int(b[1]) - 1
            if i < len(feats):
                beats.append(("benefit", feats[i]))
            elif i == 0:
                beats.append(("audience", None))
        else:
            beats.append((b, None))
    durs = split_durations(d["duration"], len(beats))

    hook_voice, hook_short = (prof[3], prof[4]) if te else (prof[1], prof[2])
    visuals = {
        "hook": f"Attention-grabbing opening: {prof[5]}. Bold kinetic text.",
        "intro": f"Hero shot of {name}{' from ' + brand if brand else ''}, slow push-in, brand colours.",
        "benefit": "Close-up demonstrating this benefit on the real product, with a clean callout label.",
        "audience": f"Show {audience} using {name} in a relatable everyday moment.",
        "demo": f"Real demonstration of {name} being used, start to finish.",
        "offer": f"Product and call to action on screen, clear arrow or button for: {cta}.",
        "close": f"End card with the {name} name, tagline and brand colours.",
    }
    scenes, start = [], 0
    for i, ((kind, feat), dur) in enumerate(zip(beats, durs)):
        if kind == "hook":
            onscreen, voice = hook_short, hook_voice
        else:
            onscreen, voice = T[kind]
            fx = {**ctx, "feature": feat or "", "feature_c": (feat or "")[:1].upper() + (feat or "")[1:].rstrip(". ")}
            onscreen, voice = onscreen.format(**fx), voice.format(**fx)
        title = {"hook": "Hook", "intro": "Introduction", "benefit": f"Benefit - {feat}" if feat else "Benefit",
                 "audience": "Audience", "demo": "Demo", "offer": "Call to action", "close": "Closing"}[kind]
        scenes.append({"id": i + 1, "start": start, "end": start + dur, "duration": dur, "title": title[:60],
                       "visual": f"{d.get('tone', 'Energetic')} tone. {visuals[kind]} Framed for {d.get('platform', 'short video')}.",
                       "voiceover": voice, "onscreen_text": onscreen, "caption": voice,
                       "transition": "fade" if i else "cut", "media": None})
        start += dur
    tag = re.sub(r"[^A-Za-z0-9]", "", name)
    cat_tag = re.sub(r"[^A-Za-z0-9]", "", p.get("category", "") or "")
    tags = ["#PromoGenAI", f"#{tag}" if tag else "", f"#{cat_tag}" if cat_tag else "", "#ShortFormVideo", "#Marketing"]
    if te:
        tags.append("#Telugu")
    return {"mode": "demo", "language": lang, "notice": notice, "product_category_profile": key,
            "campaign_title": T["title"].format(**ctx), "hook": hook_voice,
            "introduction": T["intro_para"].format(**ctx), "benefits": feats or [desc],
            "complete_script": " ".join(s["voiceover"] for s in scenes), "scenes": scenes,
            "caption": T["caption"].format(**ctx), "hashtags": [t for t in tags if t],
            "tagline": ctx["tagline"], "final_cta": cta}


def build_prompt(d):
    lang = d.get("language") or "English"
    return (
        f"Write a {d['duration']}-second {d['platform']} promotional video script for the product below. "
        f"Return ONLY one JSON object.\n"
        f"Product (JSON): {json.dumps(d['product'], ensure_ascii=False)}\n"
        f"Target audience: {d['target_audience']}\nMarketing objective: {d['marketing_objective']}\n"
        f"Tone: {d['tone']}\nCall to action: {d['call_to_action']}\nKey selling points: {d['key_selling_points']}\n"
        f"Extra instructions: {d['instructions']}\n\n"
        f"LANGUAGE RULES: write every viewer-facing field (campaign_title, hook, introduction, benefits, complete_script, "
        f"voiceover, onscreen_text, caption, tagline, final_cta) in {lang}, using its native script. "
        f"Write each scene's 'visual' direction and the hashtags in English or {lang}. Keep the product and brand names as given.\n"
        f"CONTENT RULES: use only facts present in the product details. Do not invent prices, discounts, statistics, "
        f"awards, medical or performance claims, or customer testimonials.\n\n"
        f"Required keys: campaign_title, hook (attention-grabbing opening line), introduction (1-2 sentences introducing the product), "
        f"benefits (array of 3-6 short benefit strings), complete_script (the full narration), tagline, final_cta, "
        f"caption (social post caption), hashtags (array of 6-10), scenes (array of 3-8 objects, each with: title, visual, voiceover, "
        f"onscreen_text (max 8 words), caption (subtitle text, normally the voiceover), duration (integer seconds)). "
        f"Scene durations must add up to exactly {d['duration']}. The storyboard should flow: hook, introduction, benefits, "
        f"demonstration, final call to action.")


def _s(v, limit=2000):
    return str(v if v is not None else "").strip()[:limit]


def normalize_script(raw, d, mode):
    """Repair an LLM answer. Raises ValueError (message safe to show) when it is unusable."""
    if not isinstance(raw, dict):
        raise ValueError("The AI provider returned an unexpected response. Please try again.")
    scenes = raw.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("The AI provider returned no scenes. Please try again.")
    scenes = scenes[:12]
    durs = []
    for s in scenes:
        try:
            s = s if isinstance(s, dict) else {}
            durs.append(max(1, round(float(s.get("duration") or (float(s.get("end", 0)) - float(s.get("start", 0))) or 3))))
        except (TypeError, ValueError):
            durs.append(3)
    if sum(durs) != d["duration"]:
        k = d["duration"] / sum(durs)
        durs = [max(1, round(x * k)) for x in durs]
        durs[-1] = max(1, d["duration"] - sum(durs[:-1]))
    out, start = [], 0
    for i, (s, dur) in enumerate(zip(scenes, durs)):
        s = s if isinstance(s, dict) else {}
        voice = _s(s.get("voiceover"))
        out.append({"id": i + 1, "start": start, "end": start + dur, "duration": dur, "title": _s(s.get("title") or f"Scene {i + 1}", 80),
                    "visual": _s(s.get("visual") or s.get("visual_direction")), "voiceover": voice,
                    "onscreen_text": _s(s.get("onscreen_text") or s.get("title"), 160), "caption": _s(s.get("caption")) or voice,
                    "transition": "cut" if str(s.get("transition", "")).lower() == "cut" else "fade", "media": None})
        start += dur
    tags = raw.get("hashtags") or []
    if isinstance(tags, str):
        tags = re.findall(r"#?\w+", tags)
    tags = [t if str(t).startswith("#") else f"#{t}" for t in tags if str(t).strip()][:15]
    benefits = raw.get("benefits") or []
    if isinstance(benefits, str):
        benefits = [b.strip() for b in re.split(r"[\n;]", benefits) if b.strip()]
    benefits = [_s(b, 200) for b in benefits if str(b).strip()][:8]
    return {"mode": mode, "language": d.get("language") or "English", "notice": None,
            "campaign_title": _s(raw.get("campaign_title") or d["product"].get("name") or "Campaign", 200), "hook": _s(raw.get("hook")),
            "introduction": _s(raw.get("introduction")), "benefits": benefits,
            "complete_script": _s(raw.get("complete_script") or " ".join(s["voiceover"] for s in out), 8000), "scenes": out,
            "caption": _s(raw.get("caption")), "hashtags": tags, "tagline": _s(raw.get("tagline"), 300),
            "final_cta": _s(raw.get("final_cta") or d.get("call_to_action"), 300)}
