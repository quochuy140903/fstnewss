import os
import re
import uuid
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Any, List, Optional
import yt_dlp


URL_REGEX = re.compile(r"(https?://[^\s\)\]\>\"']+)", re.IGNORECASE)
HASHTAG_REGEX = re.compile(r"(#[a-zA-Z0-9_\u00C0-\u024F\u1E00-\u1EFF]+)")


def extract_reel_id_from_url(url: str) -> str:
    """Extracts the numeric Reel/Video ID from a Facebook/Instagram/TikTok URL."""
    raw = (url or "").strip()
    m = re.search(r"(?:reel/|reels/|videos/|watch/\?v=|video\.php\?v=|share/r/|/)(\d{8,})", raw)
    if m:
        return m.group(1)
    m2 = re.search(r"(\d{10,})", raw)
    if m2:
        return m2.group(1)
    return uuid.uuid4().hex[:8]


def unwrap_redirect_url(url: str) -> str:
    """
    Unwraps Facebook/Instagram redirect URLs (e.g. https://l.facebook.com/l.php?u=https%3A%2F%2F...)
    into the clean, direct destination URL so the user can open or copy it cleanly.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
        if "facebook.com" in parsed.netloc.lower() and parsed.path.startswith("/l.php"):
            qs = urllib.parse.parse_qs(parsed.query)
            if "u" in qs and qs["u"]:
                decoded = urllib.parse.unquote(qs["u"][0]).strip()
                p2 = urllib.parse.urlparse(decoded)
                q2 = urllib.parse.parse_qsl(p2.query, keep_blank_values=True)
                clean_q2 = [(k, v) for (k, v) in q2 if k.lower() not in ("fbclid", "utm_source")]
                return urllib.parse.urlunparse(p2._replace(query=urllib.parse.urlencode(clean_q2)))
    except Exception:
        pass
    return raw


def clean_raw_caption_text(text: str) -> str:
    """
    Removes all CTA lines ('WATCH THE FULL LEGENDARY VIDEO HERE:', 'FULL VIDEO:', 'LINK IN BIO'),
    hashtag header lines ('📌 ALGORITHM OPTIMIZED HASHTAGS:', '📌 MAX-REACH HASHTAGS:'),
    URLs, and hashtags so only the true original caption story remains.
    """
    s = URL_REGEX.sub("", text or "")
    s = HASHTAG_REGEX.sub("", s)

    cleaned_lines: List[str] = []
    for raw_line in s.splitlines():
        ln = raw_line.strip()
        if not ln:
            continue
        # Remove leading pointer/camera/pin emojis for pattern matching
        plain = re.sub(r"^[\s🎬👇👆👉📌🚨💥🔥⚡✨▶️⬇️🔗]+", "", ln).strip()
        plain_lower = plain.lower()

        # Skip CTA lines and Hashtag label lines completely
        if not plain:
            continue
        if re.match(
            r"^(?:watch\s+(?:the\s+)?full|full\s+video|watch\s+full|link\s+in\s+(?:bio|comments?)|xem\s+full|click\s+the\s+link)",
            plain_lower,
        ):
            continue
        if re.match(
            r"^(?:algorithm\s+optimized\s+hashtags?|max[\s\-]*reach\s+hashtags?|trending\s+hashtags?|hashtags?|tags?)\s*[:\-–—]*$",
            plain_lower,
        ):
            continue
        if "watch the full" in plain_lower and ("here" in plain_lower or "below" in plain_lower):
            continue

        # Clean leftover inline "FULL VIDEO:" or "📌 ... HASHTAGS:" fragments
        ln = re.sub(
            r"(?i)[\s🎬👇📌]*(?:watch\s+(?:the\s+)?full\s+(?:legendary\s+)?(?:video|sketch)\s*(?:here|below)?|full\s*video|algorithm\s+optimized\s+hashtags?|max[\s\-]*reach\s+hashtags?)\s*[:\-–—👇]*",
            "",
            ln,
        ).strip()
        # Strip leading orphaned pointer emojis like "👇 " or "🎬 "
        ln = re.sub(r"^[\s🎬👇👆👉📌]+", "", ln).strip()
        # Remove timestamp markers like [03:49] or [05:06]
        ln = re.sub(r"\s*\[\d{1,2}:\d{2}\]", "", ln).strip()
        if ln:
            cleaned_lines.append(ln)

    return "\n".join(cleaned_lines).strip()


def compose_final_caption(caption_body: str, web_link: str, hashtags: str) -> str:
    """
    Builds the exact final caption format requested by user (WITHOUT repeating the ALL-CAPS Title):
      😂 WATCH FULL VIDEO: {Link}

      {Caption Body (2 paragraphs like Screenshot 2 from line 2)}

      {Hashtag}
    """
    body = (caption_body or "").strip()
    link = (web_link or "").strip()
    tags = (hashtags or "").strip()

    # Strip any duplicate "WATCH FULL VIDEO" or ALL-CAPS😂🇺🇸 title line if accidentally present in body
    body_lines = []
    for ln in body.splitlines():
        s_ln = ln.strip()
        if re.match(r"^(?:😂|👇)\s*watch\s+(?:the\s+)?full\s+(?:video|sketch)", s_ln, re.I):
            continue
        if s_ln.startswith("😂🇺🇸") or (s_ln.isupper() and len(s_ln) > 25 and "🔥" in s_ln):
            continue
        body_lines.append(ln)
    body = "\n".join(body_lines).strip()
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    header_line = f"😂 WATCH FULL VIDEO: {link}" if link else "😂 WATCH FULL VIDEO: "

    parts = [header_line]
    if body:
        parts.append(body)
    if tags:
        parts.append(tags)
    return "\n\n".join(parts)


def build_formatted_caption(
    title: str,
    cta_link: str,
    hashtags: str,
    template: str = "😂 WATCH FULL VIDEO: {cta_link}\n\n{title}\n\n{hashtags}",
) -> str:
    """Builds the final caption in the standard (😂 WATCH FULL VIDEO: Link + Caption + Hashtag) format."""
    return compose_final_caption(caption_body=title, web_link=cta_link, hashtags=hashtags)


def _human_phrase_from_slug(cta_link: str) -> str:
    """Extracts a clean human-readable topic phrase from an article URL slug if available."""
    if not cta_link:
        return ""
    try:
        path_part = urllib.parse.urlparse(cta_link).path.strip("/")
        slug = path_part.split("/")[-1] if path_part else ""
        # Strip trailing tracking codes like -tct004-aedba68f7b7a or -r19h
        slug = re.sub(r"-(?:tct\d+|r\d+[a-z0-9]*|[a-f0-9]{8,16})(?:-[a-f0-9]{6,16})?$", "", slug, flags=re.I)
        slug = re.sub(r"-[a-z0-9]{6,12}$", "", slug, flags=re.I)
        # Fix possessive -s- in slug (e.g. snl-s -> SNL's, mckinnon-s -> McKinnon's, can-t -> can't)
        slug = re.sub(r"-s-", "'s ", slug)
        slug = re.sub(r"-t-", "'t ", slug)
        words = [w for w in re.split(r"[-_]+", slug) if w]
        if len(words) >= 4:
            phrase = " ".join(words)
            return phrase
    except Exception:
        pass
    return ""


def parse_caption_structure(raw_caption: str, fallback_title: str = "") -> Dict[str, str]:
    """
    Splits a raw Reels caption into structured components:
      - title: Original clean caption text (with CTAs, URLs, hashtags, and 'FULL VIDEO:' stripped)
      - new_title: 1-line ALL-CAPS viral Title (Line 1 of Screenshot 2)
      - caption_body: 2-paragraph engaging video caption (Line 2+ of Screenshot 2, without repeating new_title)
      - cta_link: Extracted & unwrapped sample URL inside the caption (Link Đích Mẫu để cào bài)
      - my_web_link: User's final website URL after scraping the article (Link Web Của Tôi)
      - hashtags: Space-separated #hashtags
      - formatted_caption: Combined (😂 WATCH FULL VIDEO: Link + Caption Body + Hashtag)
    """
    text = (raw_caption or "").strip()
    if not text and fallback_title:
        text = fallback_title.strip()

    urls = URL_REGEX.findall(text)
    raw_cta_link = urls[0] if urls else ""
    cta_link = unwrap_redirect_url(raw_cta_link)

    hashtags_list = HASHTAG_REGEX.findall(text)
    seen_tags = set()
    unique_tags = []
    for t in hashtags_list:
        tl = t.lower()
        if tl not in seen_tags:
            seen_tags.add(tl)
            unique_tags.append(t)
    hashtags_str = " ".join(unique_tags)

    clean_body = clean_raw_caption_text(text)
    if not clean_body and fallback_title:
        clean_body = clean_raw_caption_text(fallback_title)

    if not clean_body and cta_link:
        slug_phrase = _human_phrase_from_slug(cta_link)
        if slug_phrase:
            clean_body = slug_phrase.capitalize()

    ai_res = _builtin_us_viral_rewriter(clean_body, hashtags_str, "", cta_link=cta_link)
    return {
        "title": clean_body,
        "new_title": ai_res["new_title"],
        "caption_body": ai_res["caption_body"],
        "cta_link": cta_link,
        "my_web_link": "",
        "hashtags": ai_res["hashtags"],
        "raw_caption": text,
        "formatted_caption": ai_res["formatted_caption"],
    }


def ai_rewrite_caption(
    original_title: str,
    original_hashtags: str = "",
    cta_link: str = "",
    my_web_link: str = "",
    custom_prompt: str = "",
    ai_provider: str = "auto",
    api_key: str = "",
) -> Dict[str, str]:
    """
    Rewrites the original caption into:
      1. `new_title`: ONLY the 1-line ALL-CAPS viral Hook Title (like Line 1 of Screenshot 2:
         `😂🇺🇸 GEORGE WASHINGTON HAD A DREAM... AND IT WAS ABSOLUTELY HILARIOUS! 🤣🔥`)
      2. `caption_body`: ONLY the 2 engaging US-audience paragraphs describing the video (like Line 2+ of Screenshot 2,
         without repeating `new_title`)
      3. `formatted_caption`:
         😂 WATCH FULL VIDEO: {my_web_link}

         {caption_body}

         {hashtags}
    """
    clean_orig = clean_raw_caption_text(original_title or "")
    tags_orig = (original_hashtags or "").strip()
    sample_link = unwrap_redirect_url((cta_link or "").strip())
    final_web_link = unwrap_redirect_url((my_web_link or "").strip())

    slug_hint = _human_phrase_from_slug(sample_link)
    default_instruction = (
        "Giúp tôi viết lại tiêu đề (Title) và caption video thu hút hơn dựa trên caption gốc, "
        "mục tiêu hướng đến người Mỹ (US audience)."
    )
    prompt_to_use = (custom_prompt or "").strip() or default_instruction
    full_input_context = (
        f"Original Caption: {clean_orig}\n"
        f"Topic Hint from Link: {slug_hint}\n"
        f"Original Hashtags: {tags_orig}\n"
        f"Instruction: {prompt_to_use}"
    )
    key = (api_key or "").strip()

    # 1. Try Gemini API
    if key and (ai_provider == "gemini" or key.startswith("AIza")):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}"
            sys_prompt = (
                "You are an expert US viral social media copywriter for Facebook Reels.\n"
                "Based on the Original Caption and Topic Hint, write a viral US-audience post.\n"
                "Return ONLY valid JSON with keys: \"new_title\", \"body\", \"hashtags\".\n"
                "- \"new_title\": ONLY 1 single ALL-CAPS viral title line with emojis based on the specific story/characters "
                "(example: '😂🇺🇸 GEORGE WASHINGTON HAD A DREAM... AND IT WAS ABSOLUTELY HILARIOUS! 🤣🔥'). "
                "Do NOT use generic phrases like 'WATCH THE FULL VIDEO HERE'.\n"
                "- \"body\": 2 short, engaging paragraphs in natural American English describing what happens in the video "
                "and why the reactions/moments are hilarious (matching lines 2-3 of the example, WITHOUT repeating new_title).\n"
                "- \"hashtags\": 8-10 relevant viral #hashtags."
            )
            payload = {
                "contents": [{"parts": [{"text": f"{sys_prompt}\n\n{full_input_context}"}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.75},
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                txt = resp_data["candidates"][0]["content"]["parts"][0]["text"]
                parsed_json = json.loads(txt)
                return _format_ai_output(parsed_json, final_web_link, tags_orig)
        except Exception as e:
            print(f"Gemini API fallback: {e}")

    # 2. Try OpenAI ChatGPT API
    if key and (ai_provider == "openai" or key.startswith("sk-")):
        try:
            url = "https://api.openai.com/v1/chat/completions"
            payload = {
                "model": "gpt-4o-mini",
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are an expert US viral social media copywriter for Facebook Reels. "
                            "Return JSON with keys: "
                            "'new_title' (ONLY 1 single ALL-CAPS viral title line with emojis based on the specific story, e.g. "
                            "'😂🇺🇸 GEORGE WASHINGTON HAD A DREAM... AND IT WAS ABSOLUTELY HILARIOUS! 🤣🔥'), "
                            "'body' (2 engaging paragraphs in American English describing the video story and reactions, WITHOUT repeating new_title), "
                            "and 'hashtags' (8-10 space-separated #hashtags)."
                        ),
                    },
                    {"role": "user", "content": full_input_context},
                ],
                "temperature": 0.75,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                txt = resp_data["choices"][0]["message"]["content"]
                parsed_json = json.loads(txt)
                return _format_ai_output(parsed_json, final_web_link, tags_orig)
        except Exception as e:
            print(f"OpenAI API fallback: {e}")

    # 3. Built-in Intelligent US Viral Rewriter
    return _builtin_us_viral_rewriter(clean_orig, tags_orig, final_web_link, cta_link=sample_link)


def _format_ai_output(parsed_json: Dict[str, Any], final_web_link: str, fallback_tags: str) -> Dict[str, str]:
    hook_title = str(parsed_json.get("new_title") or "").strip().splitlines()[0].strip() if parsed_json.get("new_title") else ""
    body = str(parsed_json.get("body") or "").strip()
    hashtags = str(parsed_json.get("hashtags") or fallback_tags or "").strip()

    formatted_caption = compose_final_caption(caption_body=body, web_link=final_web_link, hashtags=hashtags)
    return {
        "new_title": hook_title,
        "caption_body": body,
        "hashtags": hashtags,
        "formatted_caption": formatted_caption,
    }


def _builtin_us_viral_rewriter(
    clean_orig: str,
    tags_orig: str,
    final_web_link: str,
    cta_link: str = "",
) -> Dict[str, str]:
    """
    Smart local US-audience copywriter that produces:
      1. `new_title` (Cột Tiêu Đề Mới): 1 single ALL-CAPS viral hook line with emojis based on the true story
         (matching Line 1 of Screenshot 2: `😂🇺🇸 ... AND IT WAS ABSOLUTELY HILARIOUS! 🤣🔥`)
      2. `caption_body`: 2 natural American English paragraphs based on the video's story (matching Line 2+ of Screenshot 2)
      3. `formatted_caption` (Cột Caption Hoàn Chỉnh):
         😂 WATCH FULL VIDEO: {final_web_link}

         {caption_body}

         {hashtags}
    """
    cleaned = clean_raw_caption_text(clean_orig)
    raw_lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]

    # Separate generic hype headers (like "PEAK SATURDAY NIGHT LIVE COMEDY GENIUS!" or "WHEN UNFILTERED ABSURDITY...")
    # from the actual story lines describing the characters and scene:
    story_lines: List[str] = []
    hype_lines: List[str] = []
    for ln in raw_lines:
        plain_ln = re.sub(r"^[^\w\"']+", "", ln).strip()
        if (
            plain_ln.isupper()
            and any(
                w in plain_ln
                for w in ("PEAK ", "WHEN UNFILTERED", "COMEDY GENIUS", "LIVE TELEVISION PERFECTION", "GREATEST ANNIVERSARY")
            )
        ):
            hype_lines.append(plain_ln)
        else:
            story_lines.append(ln)

    slug_phrase = _human_phrase_from_slug(cta_link)

    # 1. Craft the 1-line ALL-CAPS Viral Title (`new_title`) based on the specific story/slug
    hook_source = ""
    if slug_phrase:
        hook_source = slug_phrase
    elif story_lines:
        # Extract the core clause from the first real story sentence
        first_story = re.sub(r"^[^\w\"']+", "", story_lines[0]).strip()
        first_story = re.sub(r"^(?:Watch as|There is a(?:n)? (?:legendary|iconic) reason why watching)\s+", "", first_story, flags=re.I)
        clauses = re.split(r"[.!?]", first_story)
        hook_source = clauses[0].strip()
    elif hype_lines:
        hook_source = hype_lines[0].rstrip(".!?")
    else:
        hook_source = "THIS CLASSIC COMEDY SKETCH WENT COMPLETELY OFF THE RAILS"

    hook_source = re.sub(r"^when\s+", "", hook_source, flags=re.I).strip().rstrip(".!?,;:")
    if len(hook_source) > 78:
        words = hook_source.split()
        hook_source = " ".join(words[:12])

    # Choose a natural punchline suffix that doesn't duplicate words already in hook_source
    hs_lower = hook_source.lower()
    if "hilarious" in hs_lower or "crack up" in hs_lower or "laugh" in hs_lower:
        suffix = "... AND NOBODY COULD KEEP A STRAIGHT FACE! 🤣🔥"
    elif "break character" in hs_lower or "breaks character" in hs_lower or "broke" in hs_lower:
        suffix = "... AND IT WAS ABSOLUTELY HILARIOUS! 🤣🔥"
    else:
        suffix = "... AND IT TURNED INTO PURE COMEDY GOLD! 🤣🔥"

    new_title = f"😂🇺🇸 {hook_source.upper()}{suffix}"

    # 2. Craft the 2-paragraph `caption_body` (matching Screenshot 2 from Line 2 onwards, WITHOUT repeating new_title)
    if len(story_lines) >= 2:
        s1 = re.sub(r"^[\s🚨💣🎬👇]+", "", story_lines[0]).strip()
        s2 = " ".join(re.sub(r"^[\s🚨💣🎬👇]+", "", x).strip() for x in story_lines[1:]).strip()
        if not any(ch in s1 for ch in ("😂", "🤣", "👽", "👻", "🔥")):
            s1 = s1.rstrip(".!") + "! 😂"
        para1 = s1
        para2 = (
            f"{s2} The priceless reactions, impeccable comedic timing, and unexpected twists make this "
            f"an unforgettable moment from start to finish! 💀👏"
        )
    elif len(story_lines) == 1:
        s1 = re.sub(r"^[\s🚨💣🎬👇]+", "", story_lines[0]).strip()
        para1 = (
            f"{s1} What starts out as a normal setup quickly spirals into pure comedy chaos "
            f"that had everyone in the studio losing it! 😂"
        )
        para2 = (
            "The deadpan delivery, the cast's priceless reactions, and the ridiculous twists make this "
            "an unforgettable comedy moment you have to watch until the very end! 💀👏"
        )
    else:
        topic_readable = slug_phrase.capitalize() if slug_phrase else "This classic comedy sketch"
        para1 = (
            f"{topic_readable}! What starts out as a serious scene quickly turns into pure comedy chaos "
            f"when nobody on stage can keep a straight face. 😂"
        )
        para2 = (
            "The deadpan delivery, the cast's priceless reactions, and the ridiculous twists make this "
            "an unforgettable moment! 💀👏"
        )

    caption_body = f"{para1}\n\n{para2}"

    # 3. Clean & enrich hashtags
    base_tags = tags_orig.split() if tags_orig else []
    extra_us_tags = ["#SNL", "#SaturdayNightLive", "#ComedyGold", "#SketchComedy", "#FunnyMoments", "#ViralReels"]
    existing_lower = {t.lower() for t in base_tags}
    for et in extra_us_tags:
        if et.lower() not in existing_lower and len(base_tags) < 10:
            base_tags.append(et)
            existing_lower.add(et.lower())
    final_tags = " ".join(base_tags)

    formatted_caption = compose_final_caption(caption_body=caption_body, web_link=final_web_link, hashtags=final_tags)
    return {
        "new_title": new_title,
        "caption_body": caption_body,
        "hashtags": final_tags,
        "formatted_caption": formatted_caption,
    }


DATA_DIR = Path(__file__).parent / "data"
FB_COOKIES_TXT_PATH = DATA_DIR / "fb_cookies.txt"
FB_COOKIES_RAW_PATH = DATA_DIR / "fb_cookies_raw.txt"


def _parse_cookie_text_to_dict(text: str) -> Dict[str, str]:
    """Parses raw Cookie header, JSON Cookie-Editor array, or Netscape cookies.txt into a deduplicated dict."""
    cookie_pairs: Dict[str, str] = {}
    s_text = (text or "").strip()
    if not s_text:
        return cookie_pairs

    if s_text.startswith("[") and s_text.endswith("]"):
        try:
            items = json.loads(s_text)
            for item in items:
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    val = str(item.get("value") or "").strip()
                    if name:
                        cookie_pairs[name] = val
        except Exception:
            pass

    if not cookie_pairs and ("\tTRUE\t" in s_text or "\tFALSE\t" in s_text or "# Netscape" in s_text):
        for line in s_text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("\t")
            if len(parts) >= 7:
                cookie_pairs[parts[5].strip()] = parts[6].strip()

    if not cookie_pairs:
        clean_header = re.sub(r"(?i)^cookie\s*:\s*", "", s_text.replace("\n", "; ").replace("\r", ""))
        for part in clean_header.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and not k.startswith("$"):
                    cookie_pairs[k] = v

    return cookie_pairs


def save_facebook_cookies(cookie_input: str) -> Dict[str, Any]:
    os.makedirs(DATA_DIR, exist_ok=True)
    text = (cookie_input or "").strip()
    if not text:
        if FB_COOKIES_TXT_PATH.exists():
            FB_COOKIES_TXT_PATH.unlink()
        if FB_COOKIES_RAW_PATH.exists():
            FB_COOKIES_RAW_PATH.unlink()
        return {"has_cookies": False, "has_login": False, "cookie_count": 0}

    with open(FB_COOKIES_RAW_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    cookie_pairs = _parse_cookie_text_to_dict(text)
    netscape_lines = ["# Netscape HTTP Cookie File", "# Generated by Reels AI Batch Studio", ""]
    for k, v in cookie_pairs.items():
        netscape_lines.append(f".facebook.com\tTRUE\t/\tTRUE\t1893456000\t{k}\t{v}")

    with open(FB_COOKIES_TXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(netscape_lines) + "\n")

    has_login = ("c_user" in cookie_pairs and "xs" in cookie_pairs)
    return {
        "has_cookies": len(cookie_pairs) > 0,
        "has_login": has_login,
        "cookie_count": len(cookie_pairs),
        "c_user": cookie_pairs.get("c_user", ""),
    }


def get_facebook_cookie_header() -> str:
    cookie_pairs: Dict[str, str] = {}
    if FB_COOKIES_RAW_PATH.exists():
        try:
            cookie_pairs = _parse_cookie_text_to_dict(FB_COOKIES_RAW_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not cookie_pairs and FB_COOKIES_TXT_PATH.exists():
        try:
            cookie_pairs = _parse_cookie_text_to_dict(FB_COOKIES_TXT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return "; ".join(f"{k}={v}" for k, v in cookie_pairs.items())


def get_facebook_cookie_status() -> Dict[str, Any]:
    header = get_facebook_cookie_header()
    if not header:
        return {"has_cookies": False, "has_login": False, "cookie_count": 0, "c_user": "", "raw_preview": ""}
    c_user_m = re.search(r"(?:^|;\s*)c_user=([^;]+)", header)
    xs_m = re.search(r"(?:^|;\s*)xs=([^;]+)", header)
    pairs = [p for p in header.split("; ") if "=" in p]
    raw_text = ""
    if FB_COOKIES_RAW_PATH.exists():
        try:
            raw_text = FB_COOKIES_RAW_PATH.read_text(encoding="utf-8")
        except Exception:
            pass
    return {
        "has_cookies": len(pairs) > 0,
        "has_login": bool(c_user_m and xs_m),
        "cookie_count": len(pairs),
        "c_user": c_user_m.group(1) if c_user_m else "",
        "raw_preview": raw_text[:120] + ("..." if len(raw_text) > 120 else ""),
    }


def extract_strict_facebook_reel_from_html(webpage: str, video_id: str) -> Dict[str, Any]:
    """
    Strictly extracts the video stream URL, caption, and uploader ONLY from `<script>` blocks
    that belong to the exact `video_id`, completely ignoring Facebook's background NewsFeed
    and 'video_feed_unit_feed' Up-Next recommended videos so it NEVER downloads a wrong video!
    """
    import html as html_lib

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", webpage or "", re.S)
    valid_scripts: List[str] = []
    for s in scripts:
        if video_id not in s:
            continue
        if '"video_feed_unit_feed","edges"' in s:
            continue
        if (
            '"data":{"video":{"creation_story"' in s
            or '"path":["video",' in s
            or f"v={video_id}" in s
            or f'"video_id":"{video_id}"' in s
            or f'"id":"{video_id}"' in s
        ):
            valid_scripts.append(s)

    video_url = ""
    raw_caption = ""
    uploader = ""

    for s in valid_scripts:
        for pat in [
            r'"progressive_url"\s*:\s*"((?:\\.|[^"\\])+)"\s*,\s*"failure_reason"\s*:\s*null\s*,\s*"metadata"\s*:\s*\{\s*"quality"\s*:\s*"HD"',
            r'"playable_url_quality_hd"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"browser_native_hd_url"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"hd_src_no_ratelimit"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"hd_src"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"progressive_url"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"playable_url"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"browser_native_sd_url"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"sd_src_no_ratelimit"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"sd_src"\s*:\s*"((?:\\.|[^"\\])+)"',
        ]:
            m_vid = re.search(pat, s)
            if m_vid:
                try:
                    candidate_v = json.loads(f'"{m_vid.group(1)}"')
                except Exception:
                    candidate_v = m_vid.group(1).replace("\\/", "/")
                if candidate_v.startswith("http"):
                    video_url = candidate_v
                    break
        if video_url:
            break

    for s in valid_scripts:
        for c_pat in [
            r'"savable_description"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"creation_story".*?"message"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"story"\s*:\s*\{[^}]*"message"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"message"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])+)"',
        ]:
            m_cap = re.search(c_pat, s)
            if m_cap:
                try:
                    raw_caption = json.loads(f'"{m_cap.group(1)}"')
                except Exception:
                    raw_caption = m_cap.group(1)
                if raw_caption:
                    break
        if raw_caption:
            break

    if not raw_caption and webpage:
        og_desc = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', webpage)
        if og_desc:
            desc_txt = html_lib.unescape(og_desc.group(1)).strip()
            if "enjoy videos and shows together" not in desc_txt.lower() and "log in to facebook" not in desc_txt.lower():
                raw_caption = desc_txt

    for s in valid_scripts:
        for u_pat in [
            r'"video_owner"\s*:\s*\{[^}]*"name"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"owner"\s*:\s*\{[^}]*"__typename"\s*:\s*"(?:User|Page)"[^}]*"name"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"actors"\s*:\s*\[\s*\{[^}]*"name"\s*:\s*"((?:\\.|[^"\\])+)"',
        ]:
            m_up = re.search(u_pat, s)
            if m_up:
                try:
                    uploader = json.loads(f'"{m_up.group(1)}"')
                except Exception:
                    uploader = m_up.group(1)
                if uploader:
                    break
        if uploader:
            break

    is_geo_or_login_blocked = (not video_url and not raw_caption) and (
        '"video":null' in (webpage or "") or "VideoHomeVideoNotFound" in (webpage or "")
    )

    return {
        "video_id": video_id,
        "video_url": video_url,
        "raw_caption": raw_caption,
        "uploader": uploader or "Fanpage Mẫu",
        "is_login_blocked": is_geo_or_login_blocked,
    }


def _direct_facebook_reel_extract(url: str, cookie_header: str = "", proxy_url: str = "") -> Dict[str, Any]:
    video_id = extract_reel_id_from_url(url)
    target_u = f"https://www.facebook.com/reel/{video_id}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        req = urllib.request.Request(target_u, headers=headers)
        if proxy_url and proxy_url.strip():
            proxy_handler = urllib.request.ProxyHandler({"http": proxy_url.strip(), "https": proxy_url.strip()})
            opener = urllib.request.build_opener(proxy_handler)
            with opener.open(req, timeout=20) as resp:
                webpage = resp.read().decode("utf-8", errors="replace")
        else:
            with urllib.request.urlopen(req, timeout=20) as resp:
                webpage = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return {
            "video_id": video_id,
            "video_url": "",
            "raw_caption": "",
            "uploader": "Fanpage Mẫu",
            "is_login_blocked": True,
        }

    return extract_strict_facebook_reel_from_html(webpage, video_id)


def download_direct_mp4_url(video_url: str, output_dir: str, filename: str) -> str:
    """Downloads a direct scontent.*.fbcdn.net MP4 stream URL to output_dir/filename."""
    os.makedirs(output_dir, exist_ok=True)
    fpath = os.path.join(output_dir, filename)
    req = urllib.request.Request(
        video_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp, open(fpath, "wb") as out_f:
        out_f.write(resp.read())
    return fpath


def _build_ydl_opts(extra_opts: Optional[Dict[str, Any]] = None, proxy_url: str = "") -> Dict[str, Any]:
    cookie_header = get_facebook_cookie_header()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Mode": "navigate",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "http_headers": headers,
    }
    if proxy_url and proxy_url.strip():
        opts["proxy"] = proxy_url.strip()
    if extra_opts:
        opts.update(extra_opts)
    return opts


def extract_reel_metadata(
    url: str,
    auto_download: bool = False,
    output_dir: Optional[str] = None,
    seq_index: Optional[int] = None,
    proxy_url: str = "",
) -> Dict[str, Any]:
    fallback_id = extract_reel_id_from_url(url)
    prefix = f"{seq_index:02d}_" if seq_index is not None else ""

    if "facebook.com" in url.lower() or "fb.watch" in url.lower():
        cookie_header = get_facebook_cookie_header()
        direct = _direct_facebook_reel_extract(url, cookie_header=cookie_header, proxy_url=proxy_url)
        if direct.get("video_url") or direct.get("raw_caption"):
            parsed = parse_caption_structure(direct["raw_caption"], fallback_title="")
            downloaded_filename = ""
            dl_status = "Chưa tải"
            if auto_download and output_dir and direct.get("video_url"):
                try:
                    fname = f"{prefix}reel_{direct['video_id']}.mp4"
                    download_direct_mp4_url(direct["video_url"], output_dir, fname)
                    downloaded_filename = fname
                    dl_status = "Đã tải về kho"
                except Exception as dl_err:
                    dl_status = f"Lỗi tải file: {str(dl_err)[:50]}"

            return {
                "status": "ready",
                "reel_id": direct["video_id"],
                "source_url": url,
                "uploader": direct.get("uploader") or "Fanpage",
                "duration": 0,
                "title": parsed["title"],
                "new_title": parsed["new_title"],
                "caption_body": parsed["caption_body"],
                "cta_link": parsed["cta_link"],
                "my_web_link": "",
                "hashtags": parsed["hashtags"],
                "raw_caption": parsed["raw_caption"],
                "formatted_caption": parsed["formatted_caption"],
                "video_filename": downloaded_filename,
                "download_status": dl_status,
                "process_status": "Chờ xử lý",
            }

    extra: Dict[str, Any] = {
        "skip_download": not auto_download,
        "extract_flat": False,
    }
    if auto_download and output_dir:
        os.makedirs(output_dir, exist_ok=True)
        extra.update({
            "outtmpl": os.path.join(output_dir, f"{prefix}reel_{fallback_id}.%(ext)s"),
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
        })

    ydl_opts = _build_ydl_opts(extra, proxy_url=proxy_url)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=bool(auto_download and output_dir))
            if not info:
                raise ValueError("Không lấy được thông tin từ URL.")

            extracted_id = str(info.get("id") or "")
            if ("facebook.com" in url.lower()) and fallback_id.isdigit() and extracted_id.isdigit() and extracted_id != fallback_id:
                raise ValueError(f"Facebook trả về video feed khác ({extracted_id} != {fallback_id}) do chặn IP Quốc gia.")

            downloaded_filename = ""
            dl_status = "Chưa tải"
            if auto_download and output_dir:
                downloaded_path = ydl.prepare_filename(info)
                if not os.path.exists(downloaded_path):
                    alt_mp4 = str(Path(downloaded_path).with_suffix(".mp4"))
                    if os.path.exists(alt_mp4):
                        downloaded_path = alt_mp4
                if os.path.exists(downloaded_path):
                    downloaded_filename = os.path.basename(downloaded_path)
                    dl_status = "Đã tải về kho"

            raw_desc = info.get("description") or info.get("title") or ""
            fallback_title = info.get("title") or ""
            parsed = parse_caption_structure(raw_desc, fallback_title=fallback_title)

            return {
                "status": "ready",
                "reel_id": fallback_id,
                "source_url": url,
                "uploader": info.get("uploader") or info.get("channel") or "Fanpage",
                "duration": info.get("duration") or 0,
                "title": parsed["title"],
                "new_title": parsed["new_title"],
                "caption_body": parsed["caption_body"],
                "cta_link": parsed["cta_link"],
                "my_web_link": "",
                "hashtags": parsed["hashtags"],
                "raw_caption": parsed["raw_caption"],
                "formatted_caption": parsed["formatted_caption"],
                "video_filename": downloaded_filename,
                "download_status": dl_status,
                "process_status": "Chờ xử lý",
            }
    except Exception as e:
        err_str = str(e)
        if "Cannot parse data" in err_str or "chặn IP" in err_str:
            friendly_err = (
                "🌐 Fanpage chặn IP Việt Nam (Bấm nút '🌐 Cào Chuẩn Qua Chrome Đang Bật VPN' ở Mục 1 để lấy đúng 100% video & caption)"
            )
        else:
            friendly_err = f"Cần kiểm tra ({err_str[:70]})"

        return {
            "status": "partial",
            "reel_id": fallback_id,
            "source_url": url,
            "uploader": "Fanpage Mẫu",
            "duration": 0,
            "title": "",
            "new_title": "",
            "caption_body": "",
            "cta_link": "",
            "my_web_link": "",
            "hashtags": "",
            "raw_caption": "",
            "formatted_caption": "",
            "video_filename": "",
            "download_status": friendly_err,
            "process_status": "Chờ xử lý",
        }


def download_reel_video(
    url: str,
    output_dir: str,
    custom_filename: Optional[str] = None,
    proxy_url: str = "",
) -> Dict[str, Any]:
    os.makedirs(output_dir, exist_ok=True)
    reel_id = extract_reel_id_from_url(url)
    safe_base = custom_filename
    if safe_base:
        safe_base = re.sub(r'[\\/*?:"<>|]', "_", Path(safe_base).stem)
    else:
        safe_base = f"reel_{reel_id}"

    if "facebook.com" in url.lower() or "fb.watch" in url.lower():
        cookie_header = get_facebook_cookie_header()
        direct = _direct_facebook_reel_extract(url, cookie_header=cookie_header, proxy_url=proxy_url)
        if direct.get("video_url"):
            fname = f"{safe_base}.mp4"
            fpath = download_direct_mp4_url(direct["video_url"], output_dir, fname)
            parsed = parse_caption_structure(direct.get("raw_caption", ""), fallback_title="")
            return {
                "video_path": fpath,
                "video_filename": fname,
                "reel_id": direct["video_id"],
                "uploader": direct.get("uploader") or "Fanpage",
                "duration": 0,
                "parsed_caption": parsed,
            }

    outtmpl = os.path.join(output_dir, f"{safe_base}.%(ext)s")
    ydl_opts = _build_ydl_opts(
        {
            "outtmpl": outtmpl,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "socket_timeout": 30,
        },
        proxy_url=proxy_url,
    )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            extracted_id = str(info.get("id") or "")
            if ("facebook.com" in url.lower()) and reel_id.isdigit() and extracted_id.isdigit() and extracted_id != reel_id:
                raise RuntimeError("Fanpage chặn IP Việt Nam (trả về video khác).")
            downloaded_path = ydl.prepare_filename(info)
            if not os.path.exists(downloaded_path):
                alt_mp4 = str(Path(downloaded_path).with_suffix(".mp4"))
                if os.path.exists(alt_mp4):
                    downloaded_path = alt_mp4

            raw_desc = info.get("description") or info.get("title") or ""
            parsed = parse_caption_structure(raw_desc, fallback_title=info.get("title") or "")

            return {
                "video_path": downloaded_path,
                "video_filename": os.path.basename(downloaded_path),
                "reel_id": reel_id,
                "uploader": info.get("uploader") or "Fanpage",
                "duration": info.get("duration") or 0,
                "parsed_caption": parsed,
            }
    except Exception as e:
        raise RuntimeError(
            "Fanpage này chặn IP Việt Nam (cần VPN Mỹ như Surfshark trên Chrome). "
            "Hãy bấm nút '🌐 Cào Chuẩn Qua Chrome Đang Bật VPN' ở Mục 1 để tải đúng 100% video gốc!"
        ) from e
