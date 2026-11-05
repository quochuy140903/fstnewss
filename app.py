import os
import io
import csv
import json
import uuid
import time
import shutil
import threading
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np
import pandas as pd
from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

from video_engine import (
    detect_old_frame_crop,
    detect_frame_viewport,
    generate_live_preview,
    process_single_video,
    get_video_metadata,
    inspect_video_metadata,
    retag_existing_video_metadata,
)
from reels_scraper import (
    parse_caption_structure,
    build_formatted_caption,
    compose_final_caption,
    extract_reel_id_from_url,
    extract_strict_facebook_reel_from_html,
    download_direct_mp4_url,
    extract_reel_metadata,
    download_reel_video,
    unwrap_redirect_url,
    ai_rewrite_caption,
    save_facebook_cookies,
    get_facebook_cookie_status,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
SHEETS_DB_PATH = DATA_DIR / "captions_table.json"

DEFAULT_INPUT_DIR = str(DATA_DIR / "kho_video_mau")
DEFAULT_FRAMES_DIR = str(DATA_DIR / "khung")
DEFAULT_OUTPUT_DIR = str(DATA_DIR / "kho_video_hoan_thanh")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

app = FastAPI(title="Reels AI Batch Studio", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_private_network_access_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        resp = JSONResponse(content={"ok": True})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return resp
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

BATCH_STATE: Dict[str, Any] = {
    "running": False,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "current_file": "",
    "logs": [],
    "results": [],
}

WRONG_RANDOM_HEX_FILES = {
    "01_reel_fa0ec3dc.mp4",
    "02_reel_9f11313a.mp4",
    "03_reel_4a074079.mp4",
    "04_reel_8f109e0a.mp4",
    "05_reel_c3d7efaf.mp4",
    "06_reel_c91a6203.mp4",
    "07_reel_644aa5ca.mp4",
    "08_reel_3f4f8d66.mp4",
    "09_reel_ef5adee4.mp4",
    "10_reel_26403700.mp4",
}


def ensure_default_setup() -> Dict[str, Any]:
    """Ensure default directories, config.json, and initial sample assets exist."""
    os.makedirs(DEFAULT_INPUT_DIR, exist_ok=True)
    os.makedirs(DEFAULT_FRAMES_DIR, exist_ok=True)
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)

    default_config = {
        "input_dir": DEFAULT_INPUT_DIR,
        "frames_dir": DEFAULT_FRAMES_DIR,
        "output_dir": DEFAULT_OUTPUT_DIR,
        "selected_frame": "",
        "sort_mode": "folder_natural",
        "custom_video_order": [],
        "custom_frame_order": [],
        "proxy_url": "",
        "caption_template": "😂 WATCH FULL VIDEO: {my_web_link}\n\n{new_title}\n\n{hashtags}",
        "ai_settings": {
            "provider": "auto",
            "api_key": "",
            "custom_prompt": "Giúp tôi viết lại tiêu đề và caption video thu hút hơn dựa trên caption trên, mục tiêu tôi hướng đến người Mỹ nên viết cho phù hợp nhé",
            "auto_rewrite_on_extract": True,
        },
        "gsheet_settings": {
            "sheet_url": "",
            "apps_script_url": "",
            "auto_sync": True,
        },
        "video_settings": {
            "auto_strip_old_frame": True,
            "override_top_pct": None,
            "override_bottom_pct": None,
            "override_left_pct": None,
            "override_right_pct": None,
            "reframe_mode": "preserve_full",
            "zoom_pct": 100.0,
            "pan_x_pct": 0.0,
            "pan_y_pct": 0.0,
            "output_width": 1080,
            "output_height": 1920,
            "box_x_pct": 0.0,
            "box_y_pct": 33.7,
            "box_w_pct": 100.0,
            "box_h_pct": 31.0,
            "border_radius": 0,
            "border_width": 0,
            "border_color": "#f5c542",
            "metadata_profile": "random",
            "metadata_strip_old": True,
            "metadata_fresh_time": True,
            "metadata_unique_hash": True,
            "metadata_custom_software": "",
            "metadata_custom_author": "",
            "metadata_custom_comment": "",
        },
    }

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if "video_settings" in saved and isinstance(saved["video_settings"], dict):
                default_config["video_settings"].update(saved.pop("video_settings"))
            default_config.update(saved)

            # Auto-relocate default data directories if project folder was moved (e.g. to D:\CV\Tool)
            relocated = False
            dir_defaults = {
                "input_dir": DEFAULT_INPUT_DIR,
                "frames_dir": DEFAULT_FRAMES_DIR,
                "output_dir": DEFAULT_OUTPUT_DIR,
            }
            for k, new_def in dir_defaults.items():
                cur_val = str(default_config.get(k, ""))
                if (
                    ("reels-batch-studio" in cur_val and "reels-batch-studio" not in str(BASE_DIR))
                    or (os.name != "nt" and (":" in cur_val or "\\" in cur_val))
                ):
                    default_config[k] = new_def
                    relocated = True
            if relocated:
                save_config(default_config)
        except Exception:
            pass
    else:
        save_config(default_config)

    for key in ("input_dir", "frames_dir", "output_dir"):
        p = default_config.get(key)
        if p:
            try:
                os.makedirs(p, exist_ok=True)
            except Exception:
                pass

    # Clean up any leftover wrong random-hex feed videos from earlier bug
    for folder_to_clean in {default_config.get("input_dir", ""), DEFAULT_INPUT_DIR}:
        if folder_to_clean and os.path.isdir(folder_to_clean):
            for bad_fname in WRONG_RANDOM_HEX_FILES:
                bad_p = os.path.join(folder_to_clean, bad_fname)
                if os.path.exists(bad_p):
                    try:
                        os.remove(bad_p)
                    except Exception:
                        pass

    if not SHEETS_DB_PATH.exists():
        save_captions_db([])

    return default_config


def save_config(cfg: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_captions_db() -> List[Dict[str, Any]]:
    import re
    ensure_default_setup()
    if not SHEETS_DB_PATH.exists():
        return []
    try:
        with open(SHEETS_DB_PATH, "r", encoding="utf-8") as f:
            rows = json.load(f)
        changed = False
        for r in rows:
            if "my_web_link" not in r:
                r["my_web_link"] = ""
                changed = True
            # If row had one of the 10 wrong random-hex feed videos, clear that corrupted feed data
            if r.get("video_filename") in WRONG_RANDOM_HEX_FILES:
                r["video_filename"] = ""
                r["title"] = ""
                r["new_title"] = ""
                r["caption_body"] = ""
                r["cta_link"] = ""
                r["raw_caption"] = ""
                r["formatted_caption"] = ""
                r["uploader"] = "Fanpage Mẫu"
                r["download_status"] = "🌐 Cần cào qua Chrome VPN (Đã xóa video ngẫu nhiên bị sai)"
                changed = True
            if r.get("cta_link") and "facebook.com/l.php" in r["cta_link"]:
                r["cta_link"] = unwrap_redirect_url(r["cta_link"])
                changed = True

            # Re-clean title & regenerate new_title (1-line Title) + caption_body (from line 2 of Screenshot 2)
            # if title still has old CTA/hashtag headers or new_title has multiple lines / "WATCH THE FULL"
            old_title_txt = str(r.get("title") or "")
            old_new_title = str(r.get("new_title") or "")
            needs_reparse = (
                "WATCH THE FULL" in old_title_txt.upper()
                or "HASHTAGS:" in old_title_txt.upper()
                or "WATCH THE FULL" in old_new_title.upper()
                or "WHEN UNFILTERED ABSURDITY" in old_new_title.upper()
                or "\n" in old_new_title.strip()
                or not r.get("caption_body")
            )
            if needs_reparse and (r.get("raw_caption") or old_title_txt):
                parsed = parse_caption_structure(r.get("raw_caption") or old_title_txt)
                r["title"] = parsed["title"]
                if not r.get("cta_link") and parsed.get("cta_link"):
                    r["cta_link"] = parsed["cta_link"]
                ai_res = ai_rewrite_caption(
                    original_title=r["title"],
                    original_hashtags=r.get("hashtags") or parsed.get("hashtags", ""),
                    cta_link=r.get("cta_link", ""),
                    my_web_link=r.get("my_web_link", ""),
                )
                r["new_title"] = ai_res["new_title"]
                r["caption_body"] = ai_res["caption_body"]
                r["hashtags"] = ai_res["hashtags"]
                r["formatted_caption"] = ai_res["formatted_caption"]
                changed = True
            elif (r.get("caption_body") or r.get("title")) and (
                not str(r.get("formatted_caption", "")).startswith("😂 WATCH FULL VIDEO:")
                or "😂🇺🇸" in str(r.get("formatted_caption", ""))
            ):
                r["formatted_caption"] = compose_final_caption(
                    caption_body=r.get("caption_body") or r.get("title") or "",
                    web_link=r.get("my_web_link") or "",
                    hashtags=r.get("hashtags") or "",
                )
                changed = True
        if changed:
            save_captions_db(rows)
        return rows
    except Exception:
        return []


def save_captions_db(rows: List[Dict[str, Any]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SHEETS_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _natural_tokens(text: str) -> List[Any]:
    """Splits text into natural sort tokens so '1' < '1 (1)' < '2' < '10'."""
    import re
    parts = re.split(r"(\d+)", text.lower())
    tokens: List[Any] = []
    for p in parts:
        if not p:
            continue
        if p.isdigit():
            tokens.append((0, int(p)))
        else:
            tokens.append((1, p))
    return tokens


def _natural_file_sort_key(rel_path: str) -> Tuple[Any, ...]:
    """
    Sorts files matching Windows Explorer folder order:
      1. Folder path in natural order (root files first, then subfolders in natural order)
      2. Filename stem in natural order ('1' before '1 (1)' before '2' before '10')
      3. Extension
    """
    p = Path(rel_path)
    parent_parts = [ _natural_tokens(part) for part in p.parent.parts if part not in (".", "") ]
    stem_tokens = _natural_tokens(p.stem)
    ext = p.suffix.lower()
    return (len(parent_parts) > 0, parent_parts, stem_tokens, ext)


def list_files_recursive(
    root_dir: str,
    allowed_exts: set,
    sort_mode: str = "folder_natural",
    custom_order: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    if not root_dir or not os.path.exists(root_dir):
        return []
    root_path = Path(root_dir)
    items = []
    for p in root_path.rglob("*"):
        if p.is_file() and p.suffix.lower() in allowed_exts:
            rel_path = str(p.relative_to(root_path)).replace("\\", "/")
            stat = p.stat()
            items.append({
                "name": p.name,
                "rel_path": rel_path,
                "full_path": str(p),
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "mtime": int(stat.st_mtime),
                "ctime": int(stat.st_ctime),
            })

    # Apply requested sort mode
    if sort_mode == "folder_natural_desc":
        items.sort(key=lambda x: _natural_file_sort_key(x["rel_path"]), reverse=True)
    elif sort_mode == "mtime_desc":
        items.sort(key=lambda x: (x["mtime"], _natural_file_sort_key(x["rel_path"])), reverse=True)
    elif sort_mode == "mtime_asc":
        items.sort(key=lambda x: (x["mtime"], _natural_file_sort_key(x["rel_path"])))
    elif sort_mode == "size_asc":
        items.sort(key=lambda x: (x["size_mb"], _natural_file_sort_key(x["rel_path"])))
    elif sort_mode == "size_desc":
        items.sort(key=lambda x: (x["size_mb"], _natural_file_sort_key(x["rel_path"])), reverse=True)
    elif sort_mode == "custom" and custom_order:
        order_map = {path: idx for idx, path in enumerate(custom_order)}
        items.sort(
            key=lambda x: (
                0 if x["rel_path"] in order_map else 1,
                order_map.get(x["rel_path"], 999999),
                _natural_file_sort_key(x["rel_path"]),
            )
        )
    else:
        # Default: folder_natural (exact Windows Explorer natural order: 1.mp4 -> 1 (1).mp4 -> 2.mp4 -> 10.mp4)
        items.sort(key=lambda x: _natural_file_sort_key(x["rel_path"]))

    return items


# ==================== API ROUTES ====================

@app.get("/", response_class=HTMLResponse)
def index():
    html_path = BASE_DIR / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/config")
def api_get_config():
    cfg = ensure_default_setup()
    sort_mode = cfg.get("sort_mode", "folder_natural")
    custom_v_order = cfg.get("custom_video_order", [])
    custom_f_order = cfg.get("custom_frame_order", [])

    videos = list_files_recursive(cfg["input_dir"], VIDEO_EXTENSIONS, sort_mode=sort_mode, custom_order=custom_v_order)
    frames = list_files_recursive(cfg["frames_dir"], IMAGE_EXTENSIONS, sort_mode=sort_mode, custom_order=custom_f_order)
    outputs = list_files_recursive(cfg["output_dir"], VIDEO_EXTENSIONS, sort_mode=sort_mode, custom_order=custom_v_order)

    if not cfg.get("selected_frame") and frames:
        cfg["selected_frame"] = frames[0]["rel_path"]
        save_config(cfg)

    return {
        "config": cfg,
        "fb_cookie_status": get_facebook_cookie_status(),
        "counts": {
            "input_videos": len(videos),
            "frames": len(frames),
            "output_videos": len(outputs),
        },
        "videos": videos,
        "frames": frames,
        "outputs": outputs,
    }


class SortOrderUpdateModel(BaseModel):
    sort_mode: str = "folder_natural"
    custom_video_order: Optional[List[str]] = None
    custom_frame_order: Optional[List[str]] = None


@app.post("/api/config/sort-order")
def api_update_sort_order(payload: SortOrderUpdateModel):
    cfg = ensure_default_setup()
    cfg["sort_mode"] = payload.sort_mode
    if payload.custom_video_order is not None:
        cfg["custom_video_order"] = payload.custom_video_order
    if payload.custom_frame_order is not None:
        cfg["custom_frame_order"] = payload.custom_frame_order
    save_config(cfg)
    return api_get_config()


class FolderUpdateModel(BaseModel):
    input_dir: str
    frames_dir: str
    output_dir: str
    create_if_missing: bool = True


@app.post("/api/config/folders")
def api_update_folders(payload: FolderUpdateModel):
    cfg = ensure_default_setup()
    for label, path_str in [
        ("Kho video mẫu", payload.input_dir),
        ("Thư mục Khung", payload.frames_dir),
        ("Kho video hoàn thành", payload.output_dir),
    ]:
        clean_p = path_str.strip().strip('"').strip("'")
        if not clean_p:
            raise HTTPException(status_code=400, detail=f"Đường dẫn {label} không được để trống.")
        if payload.create_if_missing:
            try:
                os.makedirs(clean_p, exist_ok=True)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Không thể tạo thư mục {label} ({clean_p}): {e}")
        elif not os.path.isdir(clean_p):
            raise HTTPException(status_code=400, detail=f"Thư mục {label} không tồn tại: {clean_p}")

    cfg["input_dir"] = payload.input_dir.strip().strip('"').strip("'")
    cfg["frames_dir"] = payload.frames_dir.strip().strip('"').strip("'")
    cfg["output_dir"] = payload.output_dir.strip().strip('"').strip("'")
    save_config(cfg)
    return api_get_config()


@app.post("/api/config/browse-folder")
def api_browse_folder(target: str = Form("input_dir")):
    cfg = ensure_default_setup()
    initial_dir = cfg.get(target) or str(BASE_DIR)
    try:
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$f.SelectedPath = '{initial_dir}'; "
            "$f.Description = 'Chọn thư mục cho Reels AI Batch Studio'; "
            "$f.ShowNewFolderButton = $true; "
            "if ($f.ShowDialog() -eq 'OK') { Write-Output $f.SelectedPath }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        chosen = (proc.stdout or "").strip()
        if chosen and os.path.isdir(chosen):
            cfg[target] = chosen
            save_config(cfg)
            return {"status": "selected", "path": chosen, "target": target}
        return {"status": "cancelled", "path": cfg.get(target, ""), "target": target}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không mở được hộp thoại chọn thư mục: {e}")


@app.post("/api/config/open-folder")
def api_open_folder_in_explorer(target: str = Form("output_dir")):
    cfg = ensure_default_setup()
    folder = cfg.get(target)
    if folder and os.path.isdir(folder):
        subprocess.Popen(["explorer", os.path.normpath(folder)])
        return {"status": "opened", "path": folder}
    raise HTTPException(status_code=404, detail="Thư mục không tồn tại.")


@app.post("/api/config/settings")
def api_save_video_settings(settings: Dict[str, Any]):
    cfg = ensure_default_setup()
    if "selected_frame" in settings:
        cfg["selected_frame"] = settings.pop("selected_frame")
    if "caption_template" in settings:
        cfg["caption_template"] = settings.pop("caption_template")
    cfg["video_settings"].update(settings)
    save_config(cfg)
    return {"status": "saved", "config": cfg}


# ==================== CAPTION & REELS SHEET ENDPOINTS ====================

@app.get("/api/captions")
def api_get_captions():
    return {"rows": load_captions_db()}


class AddLinksPayload(BaseModel):
    links_text: str
    auto_download: bool = False
    replace_existing: bool = False
    fb_cookies: str = ""
    proxy_url: str = ""


class BrowserExtractedItem(BaseModel):
    source_url: str
    video_id: str = ""
    video_url: str = ""
    raw_caption: str = ""
    uploader: str = ""
    html: str = ""


class BrowserImportPayload(BaseModel):
    items: List[BrowserExtractedItem]
    auto_download: bool = True
    replace_existing: bool = False


class FBCookiesPayload(BaseModel):
    cookies_text: str = ""


@app.post("/api/captions/fb-cookies")
def api_save_fb_cookies(payload: FBCookiesPayload):
    status = save_facebook_cookies(payload.cookies_text)
    return {"status": "saved", "fb_cookie_status": get_facebook_cookie_status(), "details": status}


@app.post("/api/captions/clear-all")
def api_clear_all_captions():
    save_captions_db([])
    return {"status": "cleared", "rows": []}


def _push_rows_to_gsheet_webhook(apps_script_url: str, rows: List[Dict[str, Any]], sheet_url: str = "") -> Dict[str, Any]:
    """Pushes the full table to a Google Apps Script Web App URL attached to the user's Google Sheet."""
    import urllib.request
    clean_url = (apps_script_url or "").strip()
    if not clean_url.startswith("http"):
        return {"synced": False, "reason": "Chưa cấu hình Apps Script Webhook URL"}
    if not sheet_url:
        cfg = ensure_default_setup()
        sheet_url = (cfg.get("gsheet_settings", {}).get("sheet_url") or "").strip()
    payload_bytes = json.dumps({"action": "sync_rows", "sheet_url": sheet_url, "rows": rows}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        clean_url,
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return {"synced": True, "response": body[:200]}


@app.post("/api/captions/extract-links")
def api_extract_links(payload: AddLinksPayload):
    cfg = ensure_default_setup()
    ai_cfg = cfg.get("ai_settings", {})
    gs_cfg = cfg.get("gsheet_settings", {})

    if payload.fb_cookies and payload.fb_cookies.strip():
        save_facebook_cookies(payload.fb_cookies.strip())
    if payload.proxy_url is not None and payload.proxy_url.strip():
        cfg["proxy_url"] = payload.proxy_url.strip()
        save_config(cfg)

    proxy_to_use = (payload.proxy_url or cfg.get("proxy_url") or "").strip()

    existing_rows = [] if payload.replace_existing else load_captions_db()
    existing_by_url = {r.get("source_url", "").strip(): r for r in existing_rows if r.get("source_url")}

    lines = [ln.strip() for ln in (payload.links_text or "").splitlines() if ln.strip()]
    new_ordered_batch: List[Dict[str, Any]] = []

    base_seq = 0 if payload.replace_existing else len(existing_rows)

    for idx, line in enumerate(lines, start=1):
        parts = [p.strip() for p in line.split("|", 1)]
        url = parts[0]
        manual_caption = parts[1] if len(parts) > 1 else ""

        seq_num = base_seq + idx
        meta = extract_reel_metadata(
            url,
            auto_download=payload.auto_download,
            output_dir=cfg["input_dir"],
            seq_index=seq_num,
            proxy_url=proxy_to_use,
        )
        if manual_caption:
            parsed = parse_caption_structure(manual_caption, fallback_title=meta.get("title", ""))
            meta.update(parsed)

        prev_row = existing_by_url.get(url)
        my_web_link = (prev_row.get("my_web_link", "") if prev_row else "") or meta.get("my_web_link", "")

        # Run AI rewrite for New Title (1-line Hook) & Caption Body (from line 2 of Screenshot 2)
        if meta.get("title") or meta.get("cta_link"):
            ai_res = ai_rewrite_caption(
                original_title=meta.get("title", ""),
                original_hashtags=meta.get("hashtags", ""),
                cta_link=meta.get("cta_link", ""),
                my_web_link=my_web_link,
                custom_prompt=ai_cfg.get("custom_prompt", ""),
                ai_provider=ai_cfg.get("provider", "auto"),
                api_key=ai_cfg.get("api_key", ""),
            )
            meta["new_title"] = ai_res["new_title"]
            meta["caption_body"] = ai_res["caption_body"]
            meta["hashtags"] = ai_res["hashtags"]
            meta["formatted_caption"] = ai_res["formatted_caption"]

        row_id = f"reel_{uuid.uuid4().hex[:8]}"
        new_row = {
            "id": row_id,
            "source_url": url,
            "uploader": meta.get("uploader", "Fanpage"),
            "title": meta.get("title", ""),
            "new_title": meta.get("new_title", ""),
            "caption_body": meta.get("caption_body", ""),
            "cta_link": unwrap_redirect_url(meta.get("cta_link", "")),
            "my_web_link": my_web_link,
            "hashtags": meta.get("hashtags", ""),
            "raw_caption": meta.get("raw_caption", ""),
            "formatted_caption": meta.get("formatted_caption", ""),
            "video_filename": meta.get("video_filename", "") or (prev_row.get("video_filename", "") if prev_row else ""),
            "download_status": meta.get("download_status", "Chưa tải"),
            "process_status": "Chờ xử lý",
        }
        new_ordered_batch.append(new_row)

    if payload.replace_existing:
        rows = new_ordered_batch
    else:
        rows = existing_rows + new_ordered_batch

    save_captions_db(rows)
    if gs_cfg.get("auto_sync") and gs_cfg.get("apps_script_url"):
        try:
            _push_rows_to_gsheet_webhook(gs_cfg["apps_script_url"], rows)
        except Exception:
            pass
    return {"added_count": len(new_ordered_batch), "rows": rows, "fb_cookie_status": get_facebook_cookie_status()}


@app.post("/api/captions/browser-import")
def api_browser_import_captions(payload: BrowserImportPayload):
    """
    Receives Facebook Reel data extracted directly inside the user's Chrome browser (where Surfshark US VPN is active).
    Strictly matches each `video_id`, downloads the exact `scontent.*.fbcdn.net` MP4 stream into `Kho Video Mẫu`,
    parses `Link Đích Mẫu`, rewrites the 1-line `new_title` (Title AI) + 2-paragraph `caption_body`,
    and formats `Caption Hoàn Chỉnh` as:
      😂 WATCH FULL VIDEO: {Link Web Của Tôi}

      {Caption Body}

      {Hashtags}
    """
    cfg = ensure_default_setup()
    ai_cfg = cfg.get("ai_settings", {})
    gs_cfg = cfg.get("gsheet_settings", {})

    existing_rows = [] if payload.replace_existing else load_captions_db()
    existing_by_id: Dict[str, int] = {}
    for idx_r, r in enumerate(existing_rows):
        rid = extract_reel_id_from_url(r.get("source_url", ""))
        if rid:
            existing_by_id[rid] = idx_r

    imported_count = 0
    downloaded_count = 0

    for item_idx, item in enumerate(payload.items, start=1):
        url = (item.source_url or "").strip()
        vid = (item.video_id or extract_reel_id_from_url(url)).strip()
        v_url = (item.video_url or "").strip()
        r_cap = (item.raw_caption or "").strip()
        up_name = (item.uploader or "").strip()

        if item.html and (not v_url or not r_cap):
            parsed_html = extract_strict_facebook_reel_from_html(item.html, vid)
            v_url = v_url or parsed_html.get("video_url", "")
            r_cap = r_cap or parsed_html.get("raw_caption", "")
            if not up_name or up_name == "Fanpage Mẫu":
                up_name = parsed_html.get("uploader", "Fanpage")

        if not payload.replace_existing and vid in existing_by_id:
            target_row_idx = existing_by_id[vid]
            seq_num = target_row_idx + 1
            existing_row = existing_rows[target_row_idx]
        else:
            target_row_idx = None
            seq_num = len(existing_rows) + 1
            existing_row = None

        parsed = parse_caption_structure(r_cap, fallback_title="")
        my_web_link = (existing_row.get("my_web_link", "") if existing_row else "")

        ai_res = ai_rewrite_caption(
            original_title=parsed["title"],
            original_hashtags=parsed["hashtags"],
            cta_link=parsed["cta_link"],
            my_web_link=my_web_link,
            custom_prompt=ai_cfg.get("custom_prompt", ""),
            ai_provider=ai_cfg.get("provider", "auto"),
            api_key=ai_cfg.get("api_key", ""),
        )

        fname = existing_row.get("video_filename", "") if existing_row else ""
        dl_status = "Chưa tải"
        if payload.auto_download and v_url.startswith("http"):
            try:
                fname = f"{seq_num:02d}_reel_{vid}.mp4"
                download_direct_mp4_url(v_url, cfg["input_dir"], fname)
                dl_status = "✅ Đã tải đúng video gốc (VPN Chrome)"
                downloaded_count += 1
            except Exception as dl_err:
                dl_status = f"Lỗi tải MP4: {str(dl_err)[:50]}"
        elif not v_url:
            dl_status = "⚠️ Chưa lấy được stream MP4 (Hãy kiểm tra VPN Mỹ trên Chrome)"

        row_data = {
            "id": existing_row.get("id") if existing_row else f"reel_{uuid.uuid4().hex[:8]}",
            "source_url": url or f"https://www.facebook.com/reel/{vid}",
            "uploader": up_name or (existing_row.get("uploader") if existing_row else "Fanpage"),
            "title": parsed["title"],
            "new_title": ai_res["new_title"],
            "caption_body": ai_res["caption_body"],
            "cta_link": unwrap_redirect_url(parsed["cta_link"]),
            "my_web_link": my_web_link,
            "hashtags": ai_res["hashtags"],
            "raw_caption": parsed["raw_caption"],
            "formatted_caption": ai_res["formatted_caption"],
            "video_filename": fname,
            "download_status": dl_status,
            "process_status": existing_row.get("process_status", "Chờ xử lý") if existing_row else "Chờ xử lý",
        }

        if target_row_idx is not None:
            existing_rows[target_row_idx] = row_data
        else:
            existing_rows.append(row_data)
            existing_by_id[vid] = len(existing_rows) - 1

        imported_count += 1

    save_captions_db(existing_rows)
    if gs_cfg.get("auto_sync") and gs_cfg.get("apps_script_url"):
        try:
            _push_rows_to_gsheet_webhook(gs_cfg["apps_script_url"], existing_rows)
        except Exception:
            pass

    return {
        "status": "imported",
        "imported_count": imported_count,
        "downloaded_count": downloaded_count,
        "rows": existing_rows,
    }


class RetryFailedPayload(BaseModel):
    auto_download: bool = True
    fb_cookies: str = ""
    proxy_url: str = ""


@app.post("/api/captions/retry-failed")
def api_retry_failed_captions(payload: RetryFailedPayload):
    """Re-extracts and downloads any rows that failed previously, keeping exact row order."""
    cfg = ensure_default_setup()
    ai_cfg = cfg.get("ai_settings", {})
    gs_cfg = cfg.get("gsheet_settings", {})

    if payload.fb_cookies and payload.fb_cookies.strip():
        save_facebook_cookies(payload.fb_cookies.strip())
    if payload.proxy_url and payload.proxy_url.strip():
        cfg["proxy_url"] = payload.proxy_url.strip()
        save_config(cfg)

    proxy_to_use = (payload.proxy_url or cfg.get("proxy_url") or "").strip()

    rows = load_captions_db()
    retried_count = 0

    for idx, r in enumerate(rows, start=1):
        url = (r.get("source_url") or "").strip()
        if not url.startswith("http"):
            continue
        needs_retry = (
            not r.get("title")
            or not r.get("video_filename")
            or "Lỗi" in str(r.get("download_status", ""))
            or "🔒" in str(r.get("download_status", ""))
            or "⚠️" in str(r.get("download_status", ""))
            or "🌐" in str(r.get("download_status", ""))
            or "Cần kiểm tra" in str(r.get("download_status", ""))
        )
        if not needs_retry:
            continue

        meta = extract_reel_metadata(
            url,
            auto_download=payload.auto_download,
            output_dir=cfg["input_dir"],
            seq_index=idx,
            proxy_url=proxy_to_use,
        )
        if meta.get("title") or meta.get("cta_link"):
            ai_res = ai_rewrite_caption(
                original_title=meta.get("title", ""),
                original_hashtags=meta.get("hashtags", ""),
                cta_link=meta.get("cta_link", ""),
                my_web_link=r.get("my_web_link", ""),
                custom_prompt=ai_cfg.get("custom_prompt", ""),
                ai_provider=ai_cfg.get("provider", "auto"),
                api_key=ai_cfg.get("api_key", ""),
            )
            r["uploader"] = meta.get("uploader", "Fanpage")
            r["title"] = meta.get("title", "")
            r["new_title"] = ai_res["new_title"]
            r["caption_body"] = ai_res["caption_body"]
            r["cta_link"] = unwrap_redirect_url(meta.get("cta_link", ""))
            r["hashtags"] = ai_res["hashtags"]
            r["raw_caption"] = meta.get("raw_caption", "")
            r["formatted_caption"] = ai_res["formatted_caption"]
        if meta.get("video_filename"):
            r["video_filename"] = meta["video_filename"]
        r["download_status"] = meta.get("download_status", r.get("download_status", ""))
        retried_count += 1

    save_captions_db(rows)
    if gs_cfg.get("auto_sync") and gs_cfg.get("apps_script_url"):
        try:
            _push_rows_to_gsheet_webhook(gs_cfg["apps_script_url"], rows)
        except Exception:
            pass
    return {"retried_count": retried_count, "rows": rows, "fb_cookie_status": get_facebook_cookie_status()}


@app.post("/api/captions/save")
def api_save_captions(payload: Dict[str, Any]):
    rows = payload.get("rows", [])
    for r in rows:
        if "my_web_link" not in r:
            r["my_web_link"] = ""
        if "caption_body" not in r:
            r["caption_body"] = ""
        if r.get("auto_rebuild_caption", False):
            r["formatted_caption"] = compose_final_caption(
                caption_body=r.get("caption_body") or r.get("title") or "",
                web_link=r.get("my_web_link") or "",
                hashtags=r.get("hashtags") or "",
            )
            r.pop("auto_rebuild_caption", None)

    save_captions_db(rows)
    return {"status": "saved", "rows": rows}


class AIRewritePayload(BaseModel):
    row_id: Optional[str] = None
    provider: str = "auto"
    api_key: str = ""
    custom_prompt: str = ""
    override_cta_link: str = ""


@app.post("/api/captions/ai-rewrite")
def api_ai_rewrite_captions(payload: AIRewritePayload):
    """
    Rewrites the 1-line `new_title` (Title AI) & 2-paragraph `caption_body` for either a single row
    (if row_id is specified) or all rows in the table using Gemini / ChatGPT / Built-in AI.
    Combines `my_web_link` + `caption_body` + `hashtags` into `formatted_caption`:
      😂 WATCH FULL VIDEO: {Link Web Của Tôi}

      {Caption Body (từ dòng 2 Hình 2, không lặp lại Title)}

      {Hashtag}
    """
    cfg = ensure_default_setup()
    cfg["ai_settings"] = {
        "provider": payload.provider,
        "api_key": payload.api_key.strip(),
        "custom_prompt": payload.custom_prompt.strip(),
        "auto_rewrite_on_extract": True,
    }
    save_config(cfg)

    rows = load_captions_db()
    rewritten_count = 0
    for r in rows:
        if payload.row_id and r.get("id") != payload.row_id:
            continue
        if payload.override_cta_link.strip():
            r["my_web_link"] = unwrap_redirect_url(payload.override_cta_link.strip())

        src_title = (r.get("title") or r.get("raw_caption") or "").strip()
        if not src_title and not r.get("cta_link"):
            continue

        ai_res = ai_rewrite_caption(
            original_title=src_title,
            original_hashtags=r.get("hashtags", ""),
            cta_link=r.get("cta_link", ""),
            my_web_link=r.get("my_web_link", ""),
            custom_prompt=payload.custom_prompt,
            ai_provider=payload.provider,
            api_key=payload.api_key,
        )
        r["new_title"] = ai_res["new_title"]
        r["caption_body"] = ai_res["caption_body"]
        r["hashtags"] = ai_res["hashtags"]
        r["formatted_caption"] = ai_res["formatted_caption"]
        rewritten_count += 1

    save_captions_db(rows)
    gs_cfg = cfg.get("gsheet_settings", {})
    if gs_cfg.get("auto_sync") and gs_cfg.get("apps_script_url"):
        try:
            _push_rows_to_gsheet_webhook(gs_cfg["apps_script_url"], rows)
        except Exception:
            pass
    return {"status": "rewritten", "count": rewritten_count, "rows": rows}


class GSheetSyncPayload(BaseModel):
    sheet_url: str = ""
    apps_script_url: str = ""
    auto_sync: bool = True


@app.post("/api/captions/gsheet-sync")
def api_gsheet_sync(payload: GSheetSyncPayload):
    """Saves Google Sheet settings and pushes current table rows to the connected Google Sheet."""
    cfg = ensure_default_setup()
    cfg["gsheet_settings"] = {
        "sheet_url": payload.sheet_url.strip(),
        "apps_script_url": payload.apps_script_url.strip(),
        "auto_sync": payload.auto_sync,
    }
    save_config(cfg)
    rows = load_captions_db()

    if payload.apps_script_url.strip():
        try:
            sync_res = _push_rows_to_gsheet_webhook(payload.apps_script_url.strip(), rows)
            return {"status": "synced", "details": sync_res, "rows": rows}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Lỗi đồng bộ lên Google Sheet Apps Script: {e}")

    return {"status": "config_saved", "rows": rows}


@app.post("/api/captions/gsheet-pull")
def api_gsheet_pull(payload: GSheetSyncPayload):
    """Reads rows directly from a shared Google Sheet URL (via CSV export endpoint)."""
    import re
    import urllib.request
    cfg = ensure_default_setup()
    sheet_url = (payload.sheet_url or cfg.get("gsheet_settings", {}).get("sheet_url") or "").strip()
    if not sheet_url:
        raise HTTPException(status_code=400, detail="Vui lòng dán Link Google Sheet cần đọc dữ liệu.")

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
    if not m:
        raise HTTPException(status_code=400, detail="Link Google Sheet không đúng định dạng (/spreadsheets/d/...).")
    sheet_id = m.group(1)
    gid_match = re.search(r"[#&?]gid=(\d+)", sheet_url)
    gid = gid_match.group(1) if gid_match else "0"
    csv_export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

    try:
        req = urllib.request.Request(csv_export_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            csv_bytes = resp.read()
        df = pd.read_csv(io.BytesIO(csv_bytes)).fillna("")
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Không đọc được Google Sheet (Hãy chắc chắn Sheet đã bật Chia sẻ 'Bất kỳ ai có đường liên kết đều có thể xem'): {e}",
        )

    rows = []
    for _, item in df.iterrows():
        d = item.to_dict()
        title = str(d.get("Tiêu Đề Gốc (Title)") or d.get("Tiêu Đề (Title)") or d.get("title") or "")
        new_title = str(d.get("Tiêu Đề Mới (AI)") or d.get("Tiêu Đề") or d.get("new_title") or "")
        cta_link = unwrap_redirect_url(str(d.get("Link Đích Mẫu (Cào Bài)") or d.get("Link Đích (FULL VIDEO)") or d.get("cta_link") or ""))
        my_web_link = unwrap_redirect_url(str(d.get("Link Web Của Tôi (Link Chính)") or d.get("Link Web Của Tôi") or d.get("my_web_link") or ""))
        hashtags = str(d.get("Hashtags") or d.get("hashtags") or "")
        fmt_cap = str(d.get("Caption Hoàn Chỉnh") or d.get("formatted_caption") or "")

        if not new_title and title:
            ai_res = ai_rewrite_caption(title, hashtags, "", my_web_link=my_web_link)
            new_title = ai_res["new_title"]
            fmt_cap = fmt_cap or ai_res["formatted_caption"]
        elif not fmt_cap:
            fmt_cap = compose_final_caption(new_title or title, my_web_link, hashtags)

        rows.append({
            "id": str(d.get("ID") or d.get("id") or f"reel_{uuid.uuid4().hex[:8]}"),
            "source_url": str(d.get("Link Bài Viết Reels") or d.get("Link Reels & Fanpage") or d.get("source_url") or ""),
            "uploader": str(d.get("Fanpage Mẫu") or d.get("uploader") or "Fanpage"),
            "title": title,
            "new_title": new_title,
            "cta_link": cta_link,
            "my_web_link": my_web_link,
            "hashtags": hashtags,
            "raw_caption": str(d.get("Caption Gốc") or d.get("raw_caption") or ""),
            "formatted_caption": fmt_cap,
            "video_filename": str(d.get("File Video Mẫu") or d.get("File Video") or d.get("video_filename") or ""),
            "download_status": str(d.get("Trạng Thái Tải") or d.get("download_status") or "Từ Google Sheet"),
            "process_status": str(d.get("Trạng Thái Xử Lý Khung") or d.get("process_status") or "Chờ xử lý"),
        })

    save_captions_db(rows)
    return {"status": "pulled", "count": len(rows), "rows": rows}


@app.post("/api/captions/download-video/{row_id}")
def api_download_single_reel(row_id: str):
    cfg = ensure_default_setup()
    rows = load_captions_db()
    target_idx = next((i for i, r in enumerate(rows) if r.get("id") == row_id), None)
    if target_idx is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy dòng dữ liệu.")
    target_row = rows[target_idx]

    url = target_row.get("source_url", "").strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Link Reels không hợp lệ.")

    reel_id = extract_reel_id_from_url(url)
    default_seq_fname = f"{target_idx + 1:02d}_reel_{reel_id}"

    try:
        dl = download_reel_video(
            url,
            cfg["input_dir"],
            custom_filename=target_row.get("video_filename") or default_seq_fname,
            proxy_url=cfg.get("proxy_url", ""),
        )
        target_row["video_filename"] = dl["video_filename"]
        target_row["download_status"] = "Đã tải về kho"
        if not target_row.get("title") and dl["parsed_caption"]["title"]:
            target_row["title"] = dl["parsed_caption"]["title"]
            target_row["cta_link"] = dl["parsed_caption"]["cta_link"]
            target_row["hashtags"] = dl["parsed_caption"]["hashtags"]
        save_captions_db(rows)
        return {"status": "downloaded", "row": target_row}
    except Exception as e:
        target_row["download_status"] = f"🌐 {str(e)[:90]}"
        save_captions_db(rows)
        raise HTTPException(status_code=500, detail=f"{e}")


@app.get("/api/captions/export")
def api_export_captions(fmt: str = "xlsx"):
    rows = load_captions_db()
    export_data = []
    for idx, r in enumerate(rows, start=1):
        export_data.append({
            "STT": idx,
            "ID": r.get("id", ""),
            "Link Bài Viết Reels": r.get("source_url", ""),
            "Fanpage Mẫu": r.get("uploader", ""),
            "Tiêu Đề Gốc (Title)": r.get("title", ""),
            "Link Đích Mẫu (Cào Bài)": r.get("cta_link", ""),
            "Link Web Của Tôi (Link Chính)": r.get("my_web_link", ""),
            "Tiêu Đề & Caption Mới (AI)": r.get("new_title", ""),
            "Caption Hoàn Chỉnh": r.get("formatted_caption", ""),
            "Hashtags": r.get("hashtags", ""),
            "File Video Mẫu": r.get("video_filename", ""),
            "Trạng Thái Tải": r.get("download_status", ""),
            "Trạng Thái Xử Lý Khung": r.get("process_status", ""),
        })

    df = pd.DataFrame(export_data)
    if fmt.lower() == "csv":
        stream = io.StringIO()
        df.to_csv(stream, index=False, encoding="utf-8-sig")
        raw_bytes = stream.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(raw_bytes),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="quan_ly_caption_reels.csv"'},
        )
    else:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Captions_Reels")
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="quan_ly_caption_reels.xlsx"'},
        )


@app.post("/api/captions/import")
async def api_import_captions(file: UploadFile = File(...)):
    content = await file.read()
    filename = (file.filename or "").lower()
    if filename.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(content))
    elif filename.endswith(".xlsx") or filename.endswith(".xls"):
        df = pd.read_excel(io.BytesIO(content))
    else:
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ file .xlsx hoặc .csv")

    df = df.fillna("")
    rows = []
    for _, item in df.iterrows():
        d = item.to_dict()
        title = str(d.get("Tiêu Đề Gốc (Title)") or d.get("Tiêu Đề (Title)") or d.get("title") or "")
        new_title = str(d.get("Tiêu Đề & Caption Mới (AI)") or d.get("Tiêu Đề Mới (AI)") or d.get("new_title") or "")
        cta_link = str(d.get("Link Đích Mẫu (Cào Bài)") or d.get("Link Đích (FULL VIDEO)") or d.get("cta_link") or "")
        my_web_link = str(d.get("Link Web Của Tôi (Link Chính)") or d.get("Link Web Của Tôi") or d.get("my_web_link") or "")
        hashtags = str(d.get("Hashtags") or d.get("hashtags") or "")
        raw_cap = str(d.get("Caption Gốc") or d.get("raw_caption") or "")
        fmt_cap = str(d.get("Caption Hoàn Chỉnh") or d.get("formatted_caption") or "")

        if not title and raw_cap:
            parsed = parse_caption_structure(raw_cap)
            title = parsed["title"]
            cta_link = cta_link or parsed["cta_link"]
            hashtags = hashtags or parsed["hashtags"]
        if not fmt_cap:
            fmt_cap = compose_final_caption(new_title or title, my_web_link, hashtags)

        rows.append({
            "id": str(d.get("ID") or d.get("id") or f"reel_{uuid.uuid4().hex[:8]}"),
            "source_url": str(d.get("Link Bài Viết Reels") or d.get("source_url") or ""),
            "uploader": str(d.get("Fanpage Mẫu") or d.get("uploader") or "Fanpage"),
            "title": title,
            "new_title": new_title,
            "cta_link": cta_link,
            "my_web_link": my_web_link,
            "hashtags": hashtags,
            "raw_caption": raw_cap,
            "formatted_caption": fmt_cap,
            "video_filename": str(d.get("File Video Mẫu") or d.get("File Video") or d.get("video_filename") or ""),
            "download_status": str(d.get("Trạng Thái Tải") or d.get("download_status") or "Đã nhập Sheet"),
            "process_status": str(d.get("Trạng Thái Xử Lý Khung") or d.get("process_status") or "Chờ xử lý"),
        })

    save_captions_db(rows)
    return {"status": "imported", "count": len(rows), "rows": rows}


# ==================== AI FRAME, METADATA & VIDEO PROCESSING ENDPOINTS ====================

@app.post("/api/upload-asset")
async def api_upload_asset(target: str = Form("frames_dir"), file: UploadFile = File(...)):
    cfg = ensure_default_setup()
    folder = cfg.get(target)
    if not folder:
        raise HTTPException(status_code=400, detail="Thư mục đích không hợp lệ.")
    os.makedirs(folder, exist_ok=True)
    dest_path = os.path.join(folder, file.filename)
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return api_get_config()


@app.post("/api/frame/auto-detect")
def api_auto_detect_frame(frame_rel_path: str = Form(...)):
    cfg = ensure_default_setup()
    full_frame_path = os.path.join(cfg["frames_dir"], frame_rel_path)
    if not os.path.exists(full_frame_path):
        raise HTTPException(status_code=404, detail=f"Không tìm thấy khung: {frame_rel_path}")
    detected = detect_frame_viewport(full_frame_path)
    return {"detected": detected}


class PreviewRequestModel(BaseModel):
    video_rel_path: str
    frame_rel_path: str
    settings: Dict[str, Any]


@app.post("/api/video/preview")
def api_video_preview(payload: PreviewRequestModel):
    cfg = ensure_default_setup()
    v_path = os.path.join(cfg["input_dir"], payload.video_rel_path)
    f_path = os.path.join(cfg["frames_dir"], payload.frame_rel_path)
    if not os.path.exists(v_path):
        raise HTTPException(status_code=404, detail=f"Không tìm thấy video mẫu: {payload.video_rel_path}")
    if not os.path.exists(f_path):
        raise HTTPException(status_code=404, detail=f"Không tìm thấy ảnh khung: {payload.frame_rel_path}")

    merged_settings = dict(cfg.get("video_settings", {}))
    merged_settings.update(payload.settings or {})
    cfg["video_settings"] = merged_settings
    cfg["selected_frame"] = payload.frame_rel_path
    save_config(cfg)

    preview = generate_live_preview(v_path, f_path, merged_settings)
    return preview


@app.get("/api/video/metadata")
def api_inspect_video_metadata(rel_path: str, source: str = "output_dir"):
    """Inspects container & stream metadata tags + MD5 hash of a video."""
    cfg = ensure_default_setup()
    base_folder = cfg.get(source, cfg["output_dir"])
    full_path = os.path.join(base_folder, rel_path)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail=f"Không tìm thấy video: {rel_path}")
    return inspect_video_metadata(full_path)


@app.post("/api/video/batch-retag-metadata")
def api_batch_retag_metadata(settings: Dict[str, Any]):
    """
    Fast batch Metadata cleaner & editor spoofer for all existing videos in `Kho Video Hoàn Thành`
    without re-rendering the video frames.
    """
    cfg = ensure_default_setup()
    merged_settings = dict(cfg.get("video_settings", {}))
    merged_settings.update(settings or {})
    cfg["video_settings"] = merged_settings
    save_config(cfg)

    outputs = list_files_recursive(cfg["output_dir"], VIDEO_EXTENSIONS)
    if not outputs:
        raise HTTPException(status_code=400, detail="Kho video hoàn thành đang trống.")

    rows = load_captions_db()
    updated_count = 0
    details = []
    for item in outputs:
        full_p = item["full_path"]
        fname = item["name"]
        matched_title = ""
        for r in rows:
            if r.get("video_filename") == fname and r.get("title"):
                matched_title = r["title"]
                break
        res = retag_existing_video_metadata(full_p, merged_settings, video_title=matched_title)
        updated_count += 1
        details.append({
            "file": item["rel_path"],
            "profile": res["metadata_applied"].get("profile_used", ""),
            "encoder": res["metadata_applied"].get("encoder", ""),
        })

    return {"status": "retagged_all", "count": updated_count, "details": details}


class BatchProcessRequestModel(BaseModel):
    frame_rel_path: str
    video_rel_paths: Optional[List[str]] = None
    settings: Dict[str, Any]


def _run_batch_worker(
    input_dir: str,
    output_dir: str,
    frame_path: str,
    video_rel_paths: List[str],
    settings: Dict[str, Any],
):
    BATCH_STATE["running"] = True
    BATCH_STATE["total"] = len(video_rel_paths)
    BATCH_STATE["completed"] = 0
    BATCH_STATE["failed"] = 0
    BATCH_STATE["current_file"] = ""
    BATCH_STATE["logs"] = [
        f"Bắt đầu xử lý hàng loạt {len(video_rel_paths)} video (Cắt khung cũ + AI Reframe + Làm sạch/Đổi Metadata)..."
    ]
    BATCH_STATE["results"] = []

    rows = load_captions_db()

    for idx, rel_path in enumerate(video_rel_paths, start=1):
        BATCH_STATE["current_file"] = rel_path
        in_full = os.path.join(input_dir, rel_path)
        out_rel = str(Path(rel_path).with_suffix(".mp4")).replace("\\", "/")
        out_full = os.path.join(output_dir, out_rel)

        # Match title from caption table if available
        fname = os.path.basename(rel_path)
        matched_title = ""
        for r in rows:
            if r.get("video_filename") == fname and r.get("title"):
                matched_title = r["title"]
                break

        BATCH_STATE["logs"].append(f"[{idx}/{len(video_rel_paths)}] Đang xử lý AI + Metadata: {rel_path} ...")
        try:
            res = process_single_video(in_full, frame_path, out_full, settings, video_title=matched_title)
            BATCH_STATE["completed"] += 1
            BATCH_STATE["results"].append(res)
            meta_info = res.get("metadata_applied", {})
            sw_used = meta_info.get("encoder") or meta_info.get("profile_used") or "Cleaned"
            BATCH_STATE["logs"].append(
                f"✔ Hoàn thành: {out_rel} ({res['elapsed_sec']}s | Metadata: {sw_used})"
            )
            for r in rows:
                if r.get("video_filename") == fname:
                    r["process_status"] = f"Đã hoàn thành ({sw_used[:24]})"
        except Exception as e:
            BATCH_STATE["failed"] += 1
            BATCH_STATE["logs"].append(f"✖ Lỗi {rel_path}: {str(e)}")
            for r in rows:
                if r.get("video_filename") == fname:
                    r["process_status"] = "Lỗi xử lý"

    save_captions_db(rows)
    BATCH_STATE["current_file"] = ""
    BATCH_STATE["logs"].append(
        f"=== HOÀN TẤT: Thành công {BATCH_STATE['completed']}/{BATCH_STATE['total']} video (Lỗi: {BATCH_STATE['failed']}) ==="
    )
    BATCH_STATE["running"] = False


@app.post("/api/video/batch-start")
def api_start_batch(payload: BatchProcessRequestModel):
    if BATCH_STATE["running"]:
        raise HTTPException(status_code=400, detail="Hệ thống đang chạy một tiến trình xử lý hàng loạt khác.")

    cfg = ensure_default_setup()
    frame_full = os.path.join(cfg["frames_dir"], payload.frame_rel_path)
    if not os.path.exists(frame_full):
        raise HTTPException(status_code=404, detail="Không tìm thấy file Khung đã chọn.")

    sort_mode = cfg.get("sort_mode", "folder_natural")
    custom_v_order = cfg.get("custom_video_order", [])
    all_videos = list_files_recursive(cfg["input_dir"], VIDEO_EXTENSIONS, sort_mode=sort_mode, custom_order=custom_v_order)
    if payload.video_rel_paths:
        selected = payload.video_rel_paths
    else:
        selected = [v["rel_path"] for v in all_videos]

    if not selected:
        raise HTTPException(status_code=400, detail="Kho video mẫu đang trống, chưa có video để xử lý.")

    merged_settings = dict(cfg.get("video_settings", {}))
    merged_settings.update(payload.settings or {})
    cfg["video_settings"] = merged_settings
    cfg["selected_frame"] = payload.frame_rel_path
    save_config(cfg)

    worker = threading.Thread(
        target=_run_batch_worker,
        args=(cfg["input_dir"], cfg["output_dir"], frame_full, selected, merged_settings),
        daemon=True,
    )
    worker.start()
    return {"status": "started", "total": len(selected)}


@app.get("/api/video/batch-status")
def api_get_batch_status():
    return BATCH_STATE


@app.get("/api/media/output")
def api_serve_output_video(rel_path: str):
    cfg = ensure_default_setup()
    full_p = os.path.join(cfg["output_dir"], rel_path)
    if not os.path.exists(full_p):
        raise HTTPException(status_code=404, detail="File không tồn tại.")
    return FileResponse(full_p, media_type="video/mp4", filename=os.path.basename(full_p))


@app.get("/api/media/frame")
def api_serve_frame_image(rel_path: str):
    cfg = ensure_default_setup()
    full_p = os.path.join(cfg["frames_dir"], rel_path)
    if not os.path.exists(full_p):
        raise HTTPException(status_code=404, detail="File không tồn tại.")
    return FileResponse(full_p)
