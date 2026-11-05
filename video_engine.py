import os
import sys
import cv2
import json
import math
import time
import uuid
import base64
import random
import hashlib
import shutil
import tempfile
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import numpy as np
from PIL import Image, ImageDraw


METADATA_PROFILES: Dict[str, Dict[str, str]] = {
    "canva": {
        "label": "Canva Pro Video Suite",
        "encoder": "Canva Video Export Engine 3.18.0",
        "software": "Canva Pro (Web Media Renderer)",
        "handler_name": "Canva Video Media Handler",
        "comment": "Exported with Canva",
        "vendor_id": "CNVA",
    },
    "capcut": {
        "label": "CapCut Desktop Pro",
        "encoder": "CapCut Video Editor 4.6.0 (Windows)",
        "software": "CapCut Desktop 4.6.0.1892",
        "handler_name": "CapCut Video Media Handler",
        "comment": "Created with CapCut",
        "vendor_id": "CPCT",
    },
    "premiere": {
        "label": "Adobe Premiere Pro 2025",
        "encoder": "Adobe Media Encoder 25.1.0 (Windows)",
        "software": "Adobe Premiere Pro 2025.1 (Windows)",
        "handler_name": "Mainconcept MP4 Video Media Handler",
        "comment": "Rendered by Adobe Premiere Pro",
        "vendor_id": "ADBE",
    },
    "davinci": {
        "label": "DaVinci Resolve Studio 19",
        "encoder": "Blackmagic Design DaVinci Resolve Studio 19.1",
        "software": "DaVinci Resolve Studio 19.1.2",
        "handler_name": "DaVinci Resolve Video Handler",
        "comment": "Processed with DaVinci Resolve Studio",
        "vendor_id": "BMD ",
    },
    "finalcut": {
        "label": "Apple Final Cut Pro / macOS",
        "encoder": "Apple Final Cut Pro 10.8.1",
        "software": "Final Cut Pro 10.8.1 (macOS Sonoma)",
        "handler_name": "Core Media Video",
        "comment": "Apple ProApps QuickTime",
        "vendor_id": "appl",
    },
    "iphone": {
        "label": "Apple iPhone 16 Pro Max (iOS 18)",
        "encoder": "Apple iOS 18.2",
        "software": "18.2",
        "handler_name": "Core Media Video",
        "comment": "iPhone 16 Pro Max",
        "vendor_id": "appl",
    },
}


def get_ffmpeg_exe() -> str:
    """Locate system ffmpeg or fallback to imageio-ffmpeg binary."""
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def build_metadata_ffmpeg_args(settings: Dict[str, Any], video_title: str = "") -> Tuple[List[str], Dict[str, str]]:
    """
    Builds FFmpeg arguments to:
      1. Completely strip all source metadata, chapters, and default FFmpeg/Lavf signatures.
      2. Inject fresh software/editor metadata (Canva, CapCut, Adobe Premiere, DaVinci, iPhone, Random, or Custom).
      3. Randomize creation_time slightly so batch files don't share identical timestamps.
    """
    profile_key = str(settings.get("metadata_profile", "random")).lower()
    strip_old = settings.get("metadata_strip_old", True)
    fresh_time = settings.get("metadata_fresh_time", True)

    args: List[str] = []
    applied_meta: Dict[str, str] = {}

    if strip_old:
        args.extend([
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-fflags", "+bitexact",
        ])

    if profile_key == "none":
        return args, {"profile": "none"}

    if profile_key == "clean":
        # Strip everything and leave blank encoder
        args.extend([
            "-metadata", "encoder=",
            "-metadata", "software=",
            "-metadata", "comment=",
        ])
        return args, {"profile": "clean (Đã xóa trắng 100% Metadata)"}

    if profile_key == "random":
        chosen_key = random.choice(list(METADATA_PROFILES.keys()))
        prof = dict(METADATA_PROFILES[chosen_key])
    elif profile_key in METADATA_PROFILES:
        chosen_key = profile_key
        prof = dict(METADATA_PROFILES[profile_key])
    else:
        chosen_key = "custom"
        prof = {
            "label": "Custom Metadata",
            "encoder": str(settings.get("metadata_custom_software") or "Canva Video Export Engine 3.18.0"),
            "software": str(settings.get("metadata_custom_software") or "Canva Pro"),
            "handler_name": "Video Media Handler",
            "comment": str(settings.get("metadata_custom_comment") or ""),
            "vendor_id": "appl",
        }

    # Allow user custom overrides even when a preset is selected
    custom_sw = str(settings.get("metadata_custom_software") or "").strip()
    custom_author = str(settings.get("metadata_custom_author") or "").strip()
    custom_comment = str(settings.get("metadata_custom_comment") or "").strip()

    if custom_sw:
        prof["encoder"] = custom_sw
        prof["software"] = custom_sw
    if custom_comment:
        prof["comment"] = custom_comment

    # Generate realistic recent creation timestamp (offset by random 1..900 seconds in batch)
    if fresh_time:
        jitter_sec = random.randint(2, 600)
        dt_utc = datetime.now(timezone.utc) - timedelta(seconds=jitter_sec)
        creation_iso = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        creation_iso = ""

    meta_pairs = [
        ("encoder", prof["encoder"]),
        ("software", prof["software"]),
        ("creation_tool", prof["software"]),
        ("comment", prof["comment"]),
    ]
    if creation_iso:
        meta_pairs.append(("creation_time", creation_iso))
        meta_pairs.append(("date", creation_iso[:10]))
    if custom_author:
        meta_pairs.append(("artist", custom_author))
        meta_pairs.append(("author", custom_author))
        meta_pairs.append(("copyright", f"© {datetime.now().year} {custom_author}"))
    if video_title:
        meta_pairs.append(("title", video_title[:120]))

    for k, v in meta_pairs:
        if v is not None:
            args.extend(["-metadata", f"{k}={v}"])
            applied_meta[k] = v

    # Stream-level metadata so video/audio tracks also carry the editor handler instead of Lavc/Lavf
    handler = prof.get("handler_name", "Video Media Handler")
    args.extend([
        "-metadata:s:v:0", f"handler_name={handler}",
        "-metadata:s:v:0", f"encoder={prof['encoder']}",
        "-metadata:s:a:0", "handler_name=Sound Media Handler",
    ])
    applied_meta["profile_used"] = prof.get("label", chosen_key)
    applied_meta["handler_name"] = handler

    return args, applied_meta


def inspect_video_metadata(video_path: str) -> Dict[str, Any]:
    """
    Inspects the actual container tags, stream tags, file size, and MD5 checksum of a video
    using FFmpeg stderr output so the user can verify the cleaned/spoofed metadata on the Web UI.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Không tìm thấy file: {video_path}")

    ffmpeg_exe = get_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-i", video_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    raw_info = proc.stderr or ""

    # Parse Metadata section lines from ffmpeg -i output
    parsed_tags: Dict[str, str] = {}
    in_meta = False
    for line in raw_info.splitlines():
        stripped = line.strip()
        if stripped.startswith("Metadata:"):
            in_meta = True
            continue
        if in_meta:
            if stripped.startswith("Duration:") or stripped.startswith("Stream #") or stripped.startswith("At least one"):
                in_meta = False
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                k_clean = k.strip()
                v_clean = v.strip()
                if k_clean and k_clean not in parsed_tags:
                    parsed_tags[k_clean] = v_clean

    # Compute fast MD5 hash of the file
    h_md5 = hashlib.md5()
    with open(video_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h_md5.update(chunk)

    stat = os.stat(video_path)
    return {
        "filename": os.path.basename(video_path),
        "size_mb": round(stat.st_size / (1024 * 1024), 2),
        "md5_hash": h_md5.hexdigest(),
        "modified_time": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "tags": parsed_tags,
        "raw_ffmpeg_info": raw_info,
    }


def get_video_metadata(video_path: str) -> Dict[str, Any]:
    """Read basic video properties using OpenCV."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Không thể mở video: {video_path}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if fps > 0 else 0.0
    cap.release()
    return {
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "frame_count": frame_count,
        "duration": round(duration, 2),
    }


def sample_video_frames(video_path: str, num_samples: int = 16) -> Tuple[List[np.ndarray], Dict[str, Any]]:
    """Sample evenly spaced frames from a video for AI motion & face analysis."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Không thể mở video: {video_path}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if fps > 0 else 0.0

    frames = []
    if frame_count > 2:
        indices = np.linspace(
            max(0, int(frame_count * 0.05)),
            max(1, int(frame_count * 0.95) - 1),
            num=min(num_samples, max(2, frame_count)),
            dtype=int,
        )
        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
            ret, frame = cap.read()
            if ret and frame is not None:
                frames.append(frame)
    else:
        for _ in range(num_samples):
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            frames.append(frame)

    cap.release()
    meta = {
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "frame_count": frame_count,
        "duration": round(duration, 2),
    }
    return frames, meta


def detect_old_frame_crop(video_path: str) -> Dict[str, Any]:
    """
    AI / Computer Vision detector that strips out old static Reels frames (top/bottom/left/right banners)
    by combining temporal motion analysis across frames with horizontal/vertical boundary seam detection.
    """
    frames, meta = sample_video_frames(video_path, num_samples=16)
    h, w = meta["height"], meta["width"]
    if not frames or h == 0 or w == 0:
        return {
            "top_pct": 0.0,
            "bottom_pct": 0.0,
            "left_pct": 0.0,
            "right_pct": 0.0,
            "crop_x": 0,
            "crop_y": 0,
            "crop_w": w,
            "crop_h": h,
            "video_w": w,
            "video_h": h,
            "duration": meta["duration"],
        }

    scale_w = 360
    scale_h = max(64, int(h * (scale_w / w)))
    small_gray = [
        cv2.GaussianBlur(cv2.cvtColor(cv2.resize(f, (scale_w, scale_h)), cv2.COLOR_BGR2GRAY), (3, 3), 0)
        for f in frames
    ]

    top_pct, bottom_pct, left_pct, right_pct = 0.0, 0.0, 0.0, 0.0

    if len(small_gray) >= 3:
        stack = np.stack(small_gray, axis=0).astype(np.float32)
        temporal_std = np.std(stack, axis=0)
        diffs = np.mean(np.abs(np.diff(stack, axis=0)), axis=0)
        motion_map = temporal_std * 0.65 + diffs * 0.35

        row_motion = np.percentile(motion_map[:, int(scale_w * 0.05):int(scale_w * 0.95)], 95, axis=1)
        col_motion = np.percentile(motion_map[int(scale_h * 0.15):int(scale_h * 0.85), :], 95, axis=0)

        max_row_m = float(np.max(row_motion)) if len(row_motion) else 0.0
        max_col_m = float(np.max(col_motion)) if len(col_motion) else 0.0

        if max_row_m > 1.2:
            thresh_r = max(4.2, max_row_m * 0.14)
            active_rows = np.where(row_motion > thresh_r)[0]
            if len(active_rows) > int(scale_h * 0.18):
                r_start = int(active_rows[0])
                r_end = int(active_rows[-1])

                mid_frame = small_gray[len(small_gray) // 2]
                sobel_y = np.mean(np.abs(cv2.Sobel(mid_frame, cv2.CV_32F, 0, 1, ksize=3)), axis=1)
                search_win = max(4, int(scale_h * 0.02))
                if r_start > search_win:
                    s_low = max(0, r_start - 1)
                    s_high = min(scale_h - 1, r_start + search_win)
                    r_start = s_low + int(np.argmax(sobel_y[s_low:s_high + 1]))
                if r_end < scale_h - search_win:
                    s_low = max(0, r_end - search_win)
                    s_high = min(scale_h - 1, r_end + 1)
                    r_end = s_low + int(np.argmax(sobel_y[s_low:s_high + 1]))

                top_pct = round((r_start / scale_h) * 100.0, 2)
                bottom_pct = round(((scale_h - 1 - r_end) / scale_h) * 100.0, 2)

        if max_col_m > 1.2:
            thresh_c = max(3.5, max_col_m * 0.12)
            active_cols = np.where(col_motion > thresh_c)[0]
            if len(active_cols) > int(scale_w * 0.30):
                c_start = int(active_cols[0])
                c_end = int(active_cols[-1])
                cand_left = round((c_start / scale_w) * 100.0, 2)
                cand_right = round(((scale_w - 1 - c_end) / scale_w) * 100.0, 2)
                # Only crop left/right if BOTH sides have symmetric static pillarbox borders
                if cand_left >= 2.5 and cand_right >= 2.5 and abs(cand_left - cand_right) <= 3.5:
                    left_pct = cand_left
                    right_pct = cand_right

    if top_pct < 3.0 and bottom_pct < 3.0 and h > w:
        mid_frame = small_gray[len(small_gray) // 2]
        sobel_y = np.mean(np.abs(cv2.Sobel(mid_frame, cv2.CV_32F, 0, 1, ksize=3)), axis=1)
        top_zone = sobel_y[int(scale_h * 0.15):int(scale_h * 0.42)]
        bot_zone = sobel_y[int(scale_h * 0.58):int(scale_h * 0.85)]
        mean_edge = float(np.mean(sobel_y)) + 1e-5
        if len(top_zone) and float(np.max(top_zone)) > mean_edge * 2.2:
            t_idx = int(scale_h * 0.15) + int(np.argmax(top_zone))
            top_pct = round((t_idx / scale_h) * 100.0, 2)
        if len(bot_zone) and float(np.max(bot_zone)) > mean_edge * 2.2:
            b_idx = int(scale_h * 0.58) + int(np.argmax(bot_zone))
            bottom_pct = round(((scale_h - 1 - b_idx) / scale_h) * 100.0, 2)

    top_pct = max(0.0, min(45.0, top_pct))
    bottom_pct = max(0.0, min(45.0, bottom_pct))
    left_pct = max(0.0, min(30.0, left_pct))
    right_pct = max(0.0, min(30.0, right_pct))

    if top_pct >= 5.0:
        top_pct = min(45.0, round(top_pct + 0.4, 2))
    if bottom_pct >= 5.0:
        bottom_pct = min(45.0, round(bottom_pct + 0.4, 2))

    crop_x = int(round(w * (left_pct / 100.0)))
    crop_y = int(round(h * (top_pct / 100.0)))
    crop_w = max(64, int(round(w * (1.0 - (left_pct + right_pct) / 100.0))))
    crop_h = max(64, int(round(h * (1.0 - (top_pct + bottom_pct) / 100.0))))

    return {
        "top_pct": top_pct,
        "bottom_pct": bottom_pct,
        "left_pct": left_pct,
        "right_pct": right_pct,
        "crop_x": crop_x,
        "crop_y": crop_y,
        "crop_w": crop_w,
        "crop_h": crop_h,
        "video_w": w,
        "video_h": h,
        "duration": meta["duration"],
    }


def detect_ai_subject_center(
    frames: List[np.ndarray],
    crop_x: int,
    crop_y: int,
    crop_w: int,
    crop_h: int,
) -> Tuple[float, float, List[Dict[str, int]]]:
    """
    Multi-Cue AI Face & Human Subject Detector inside the cropped active video region.
    """
    if not frames or crop_w <= 0 or crop_h <= 0:
        return 0.5, 0.42, []

    centers_x = []
    centers_y = []
    weights = []
    detected_boxes = []

    step = max(1, len(frames) // 8)
    sampled = frames[::step][:8]
    mid_sample_idx = len(sampled) // 2

    for idx, frame in enumerate(sampled):
        fh, fw = frame.shape[:2]
        x1 = max(0, min(fw - 1, crop_x))
        y1 = max(0, min(fh - 1, crop_y))
        x2 = max(x1 + 1, min(fw, crop_x + crop_w))
        y2 = max(y1 + 1, min(fh, crop_y + crop_h))
        roi = frame[y1:y2, x1:x2]
        rh, rw = roi.shape[:2]
        if rw < 32 or rh < 32:
            continue

        ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        skin_mask = cv2.inRange(ycrcb, np.array([35, 130, 75]), np.array([255, 185, 140]))

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        skin_mask = cv2.morphologyEx(skin_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        skin_mask = cv2.morphologyEx(skin_mask, cv2.MORPH_OPEN, kernel, iterations=1)

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 140)

        contours, _ = cv2.findContours(skin_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:4]:
            bx, by, bw, bh = cv2.boundingRect(cnt)
            area_ratio = (bw * bh) / float(rw * rh)
            aspect = bw / max(1.0, float(bh))
            if 0.012 < area_ratio < 0.35 and 0.45 <= aspect <= 1.65 and by < rh * 0.65:
                sub_edge = edges[by:by + bh, bx:bx + bw]
                edge_density = float(np.mean(sub_edge > 0)) if sub_edge.size else 0.0
                if edge_density > 0.02:
                    cx_norm = (bx + bw * 0.5) / rw
                    cy_norm = (by + bh * 0.55) / rh
                    w_score = area_ratio * (1.0 + edge_density * 5.0)
                    if by < rh * 0.40:
                        w_score *= 1.8

                    centers_x.append(cx_norm)
                    centers_y.append(cy_norm)
                    weights.append(w_score)

                    if idx == mid_sample_idx:
                        detected_boxes.append({
                            "x": x1 + bx,
                            "y": y1 + by,
                            "w": bw,
                            "h": bh,
                        })

    if weights:
        w_arr = np.array(weights)
        focus_x = float(np.sum(np.array(centers_x) * w_arr) / np.sum(w_arr))
        focus_y = float(np.sum(np.array(centers_y) * w_arr) / np.sum(w_arr))
        return max(0.15, min(0.85, focus_x)), max(0.15, min(0.80, focus_y)), detected_boxes

    saliency_x, saliency_y = 0.5, 0.42
    try:
        mid = sampled[mid_sample_idx]
        roi = mid[crop_y:crop_y + crop_h, crop_x:crop_x + crop_w]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 60, 150).astype(np.float32)
        if np.sum(edges) > 100:
            ys, xs = np.indices(edges.shape)
            saliency_x = float(np.sum(xs * edges) / np.sum(edges)) / edges.shape[1]
            saliency_y = float(np.sum(ys * edges) / np.sum(edges)) / edges.shape[0]
    except Exception:
        pass

    return max(0.2, min(0.8, saliency_x)), max(0.2, min(0.75, saliency_y)), []


def compute_smart_reframe_box(
    crop_x: int,
    crop_y: int,
    crop_w: int,
    crop_h: int,
    target_w: int,
    target_h: int,
    focus_x_norm: float = 0.5,
    focus_y_norm: float = 0.42,
    reframe_mode: str = "preserve_full",
    zoom_pct: float = 100.0,
    pan_x_pct: float = 0.0,
    pan_y_pct: float = 0.0,
) -> Tuple[int, int, int, int]:
    """
    Computes the source sub-rectangle from the active video region (crop_x, crop_y, crop_w, crop_h):
      - 'preserve_full' (Default) or 'stretch': Keeps 100% of the active inner video without cutting off
        any subject on the left/right/top/bottom! Also supports optional user Zoom (%) and Pan X/Y (%).
      - 'ai_smart': Crops to match target aspect ratio centered on AI-detected face/subject.
      - 'center': Crops to match target aspect ratio centered at (50%, 50%).
    """
    zoom = max(50.0, min(200.0, float(zoom_pct or 100.0))) / 100.0
    px_shift = float(pan_x_pct or 0.0) / 100.0
    py_shift = float(pan_y_pct or 0.0) / 100.0

    if reframe_mode in ("preserve_full", "stretch", "fit_contain"):
        # Keep 100% of the active video region (unless user explicitly zooms in > 100%)
        if zoom <= 1.001 and abs(px_shift) < 0.001 and abs(py_shift) < 0.001:
            return crop_x, crop_y, crop_w, crop_h
        new_w = max(32, min(crop_w, int(round(crop_w / zoom))))
        new_h = max(32, min(crop_h, int(round(crop_h / zoom))))
        cx = crop_w * (0.5 + px_shift * 0.5)
        cy = crop_h * (0.5 + py_shift * 0.5)
        sub_x = max(0, min(crop_w - new_w, int(round(cx - new_w / 2.0))))
        sub_y = max(0, min(crop_h - new_h, int(round(cy - new_h / 2.0))))
        return crop_x + sub_x, crop_y + sub_y, new_w, new_h

    src_aspect = crop_w / max(1.0, float(crop_h))
    dst_aspect = target_w / max(1.0, float(target_h))

    if reframe_mode == "center":
        focus_x_norm = 0.5
        focus_y_norm = 0.45

    focus_x_norm = max(0.0, min(1.0, focus_x_norm + px_shift * 0.5))
    focus_y_norm = max(0.0, min(1.0, focus_y_norm + py_shift * 0.5))

    if src_aspect > dst_aspect:
        new_h = max(32, int(round(crop_h / zoom)))
        new_w = max(32, min(crop_w, int(round(new_h * dst_aspect))))
        center_px = crop_w * focus_x_norm
        center_py = crop_h * focus_y_norm
        sub_x = max(0, min(crop_w - new_w, int(round(center_px - new_w / 2.0))))
        sub_y = max(0, min(crop_h - new_h, int(round(center_py - new_h / 2.0))))
        return crop_x + sub_x, crop_y + sub_y, new_w, new_h
    else:
        new_w = max(32, int(round(crop_w / zoom)))
        new_h = max(32, min(crop_h, int(round(new_w / dst_aspect))))
        center_px = crop_w * focus_x_norm
        center_py = crop_h * focus_y_norm
        sub_x = max(0, min(crop_w - new_w, int(round(center_px - new_w / 2.0))))
        sub_y = max(0, min(crop_h - new_h, int(round(center_py - new_h / 2.0))))
        return crop_x + sub_x, crop_y + sub_y, new_w, new_h


def detect_frame_viewport(frame_path: str) -> Dict[str, Any]:
    """
    Smart detector for the video viewport inside ANY frame template (.png/.jpg/.webp):
      1. Transparent cutout (alpha < 64)
      2. Solid Black / Dark placeholder window (like 'Carlton Dance & Comedy Vault.png')
      3. Uniform solid-color placeholder box (green screen / white / gray box in the middle)
      4. Gold/colored rectangular border (like 'SNL Comedy Classics')
    """
    img = Image.open(frame_path).convert("RGBA")
    w, h = img.size
    arr = np.array(img)
    alpha = arr[:, :, 3]

    # 1. Check transparent hole
    transparent_mask = (alpha < 64).astype(np.uint8)
    transparent_ratio = float(np.sum(transparent_mask)) / float(w * h)
    if 0.08 < transparent_ratio < 0.90:
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(transparent_mask, connectivity=8)
        best_area = 0
        best_box = None
        for i in range(1, num_labels):
            bx, by, bw, bh, area = stats[i]
            if area > best_area and bw > w * 0.3 and bh > h * 0.15:
                best_area = area
                best_box = (int(bx), int(by), int(bw), int(bh))
        if best_box:
            bx, by, bw, bh = best_box
            return {
                "frame_w": w,
                "frame_h": h,
                "has_alpha_hole": True,
                "box_x_pct": round(bx / w * 100.0, 2),
                "box_y_pct": round(by / h * 100.0, 2),
                "box_w_pct": round(bw / w * 100.0, 2),
                "box_h_pct": round(bh / h * 100.0, 2),
                "border_radius": 0,
                "border_width": 0,
                "border_color": "#f5c542",
            }

    rgb = arr[:, :, :3]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    # 2. Check solid black / dark window in the middle (e.g. Carlton Dance & Comedy Vault.png)
    dark_mask = (gray < 18).astype(np.uint8)
    # Zero out outer 12% top and bottom so dark graphic elements in banners don't merge
    dark_mask[:int(h * 0.12), :] = 0
    dark_mask[int(h * 0.88):, :] = 0
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(dark_mask, connectivity=8)
    best_dark_area = 0
    best_dark_box = None
    for i in range(1, num_labels):
        bx, by, bw, bh, area = stats[i]
        fill_ratio = float(area) / max(1.0, float(bw * bh))
        if area > best_dark_area and bw > w * 0.60 and bh > h * 0.15 and fill_ratio > 0.85:
            best_dark_area = area
            best_dark_box = (int(bx), int(by), int(bw), int(bh))

    if best_dark_box:
        bx, by, bw, bh = best_dark_box
        is_full_width = bw >= w * 0.96
        return {
            "frame_w": w,
            "frame_h": h,
            "has_alpha_hole": False,
            "box_x_pct": 0.0 if is_full_width else round(bx / w * 100.0, 2),
            "box_y_pct": round(by / h * 100.0, 2),
            "box_w_pct": 100.0 if is_full_width else round(bw / w * 100.0, 2),
            "box_h_pct": round(bh / h * 100.0, 2),
            "border_radius": 0 if is_full_width else 16,
            "border_width": 0,
            "border_color": "#f5c542",
        }

    # 3. Check gold/colored rectangular frame border (e.g. SNL Comedy Classics)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    gold_mask = cv2.inRange(hsv, np.array([15, 110, 130]), np.array([38, 255, 255]))
    contours, _ = cv2.findContours(gold_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        rx, ry, rw, rh = cv2.boundingRect(cnt)
        if rw > w * 0.65 and 0.20 * h < rh < 0.65 * h and 0.15 * h < ry < 0.55 * h:
            inset = max(4, int(round(w * 0.008)))
            bx = rx + inset
            by = ry + inset
            bw = rw - inset * 2
            bh = rh - inset * 2
            return {
                "frame_w": w,
                "frame_h": h,
                "has_alpha_hole": False,
                "box_x_pct": round(bx / w * 100.0, 2),
                "box_y_pct": round(by / h * 100.0, 2),
                "box_w_pct": round(bw / w * 100.0, 2),
                "box_h_pct": round(bh / h * 100.0, 2),
                "border_radius": 24,
                "border_width": 6,
                "border_color": "#f5c542",
            }

    return {
        "frame_w": w,
        "frame_h": h,
        "has_alpha_hole": False,
        "box_x_pct": 0.0,
        "box_y_pct": 33.7,
        "box_w_pct": 100.0,
        "box_h_pct": 31.0,
        "border_radius": 0,
        "border_width": 0,
        "border_color": "#f5c542",
    }


def build_prepared_overlay_png(
    frame_path: str,
    target_w: int,
    target_h: int,
    box_x: int,
    box_y: int,
    box_w: int,
    box_h: int,
    border_radius: int = 24,
    border_width: int = 6,
    border_color: str = "#f5c542",
    punch_hole: bool = True,
) -> Image.Image:
    base_img = Image.open(frame_path).convert("RGBA")
    if base_img.size != (target_w, target_h):
        base_img = base_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

    ss = 2
    mask_ss = Image.new("L", (target_w * ss, target_h * ss), 255)
    draw_ss = ImageDraw.Draw(mask_ss)

    x1, y1 = box_x * ss, box_y * ss
    x2, y2 = (box_x + box_w) * ss, (box_y + box_h) * ss
    rad = max(0, int(border_radius * ss))

    if punch_hole:
        draw_ss.rounded_rectangle([x1, y1, x2, y2], radius=rad, fill=0)
        hole_mask = mask_ss.resize((target_w, target_h), Image.Resampling.LANCZOS)

        r, g, b, orig_a = base_img.split()
        combined_a = Image.fromarray(np.minimum(np.array(orig_a), np.array(hole_mask)))
        base_img.putalpha(combined_a)

    if border_width > 0:
        border_layer_ss = Image.new("RGBA", (target_w * ss, target_h * ss), (0, 0, 0, 0))
        b_draw = ImageDraw.Draw(border_layer_ss)
        b_draw.rounded_rectangle(
            [x1, y1, x2, y2],
            radius=rad,
            outline=border_color,
            width=max(1, int(border_width * ss)),
        )
        border_layer = border_layer_ss.resize((target_w, target_h), Image.Resampling.LANCZOS)
        base_img = Image.alpha_composite(base_img, border_layer)

    return base_img


def resolve_canvas_dimensions(frame_path: str, settings: Dict[str, Any]) -> Tuple[int, int]:
    """
    Returns (out_w, out_h) for rendering.
    Always defaults to standard 9:16 Facebook Reels (1080x1920) with square pixels (SAR 1:1).
    """
    out_w = int(settings.get("output_width") or 1080)
    out_h = int(settings.get("output_height") or 1920)
    out_w = max(64, out_w - (out_w % 2))
    out_h = max(64, out_h - (out_h % 2))
    return out_w, out_h


def generate_live_preview(
    video_path: str,
    frame_path: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    frames, meta = sample_video_frames(video_path, num_samples=12)
    if not frames:
        raise ValueError("Không đọc được khung hình từ video mẫu.")

    mid_frame = frames[len(frames) // 2].copy()
    vh, vw = mid_frame.shape[:2]

    if settings.get("auto_strip_old_frame", True):
        auto_crop = detect_old_frame_crop(video_path)
        top_pct = float(settings.get("override_top_pct") if settings.get("override_top_pct") is not None else auto_crop["top_pct"])
        bottom_pct = float(settings.get("override_bottom_pct") if settings.get("override_bottom_pct") is not None else auto_crop["bottom_pct"])
        left_pct = float(settings.get("override_left_pct") if settings.get("override_left_pct") is not None else auto_crop["left_pct"])
        right_pct = float(settings.get("override_right_pct") if settings.get("override_right_pct") is not None else auto_crop["right_pct"])
    else:
        top_pct = float(settings.get("override_top_pct") or 0.0)
        bottom_pct = float(settings.get("override_bottom_pct") or 0.0)
        left_pct = float(settings.get("override_left_pct") or 0.0)
        right_pct = float(settings.get("override_right_pct") or 0.0)

    crop_x = int(round(vw * (left_pct / 100.0)))
    crop_y = int(round(vh * (top_pct / 100.0)))
    crop_w = max(64, int(round(vw * (1.0 - (left_pct + right_pct) / 100.0))))
    crop_h = max(64, int(round(vh * (1.0 - (top_pct + bottom_pct) / 100.0))))

    reframe_mode = settings.get("reframe_mode", "preserve_full")
    zoom_pct = float(settings.get("zoom_pct") or 100.0)
    pan_x_pct = float(settings.get("pan_x_pct") or 0.0)
    pan_y_pct = float(settings.get("pan_y_pct") or 0.0)

    focus_x, focus_y, face_boxes = detect_ai_subject_center(frames, crop_x, crop_y, crop_w, crop_h)
    if settings.get("manual_focus_x") is not None:
        focus_x = float(settings["manual_focus_x"])
    if settings.get("manual_focus_y") is not None:
        focus_y = float(settings["manual_focus_y"])

    out_w, out_h = resolve_canvas_dimensions(frame_path, settings)
    box_x = int(round(out_w * (float(settings.get("box_x_pct", 0.0)) / 100.0)))
    box_y = int(round(out_h * (float(settings.get("box_y_pct", 33.7)) / 100.0)))
    box_w = max(64, int(round(out_w * (float(settings.get("box_w_pct", 100.0)) / 100.0))))
    box_h = max(64, int(round(out_h * (float(settings.get("box_h_pct", 31.0)) / 100.0))))

    box_w = box_w - (box_w % 2)
    box_h = box_h - (box_h % 2)

    sub_x, sub_y, sub_w, sub_h = compute_smart_reframe_box(
        crop_x, crop_y, crop_w, crop_h,
        box_w, box_h,
        focus_x_norm=focus_x,
        focus_y_norm=focus_y,
        reframe_mode=reframe_mode,
        zoom_pct=zoom_pct,
        pan_x_pct=pan_x_pct,
        pan_y_pct=pan_y_pct,
    )

    diag = mid_frame.copy()
    dark = (diag * 0.35).astype(np.uint8)
    dark[crop_y:crop_y + crop_h, crop_x:crop_x + crop_w] = diag[crop_y:crop_y + crop_h, crop_x:crop_x + crop_w]
    diag = dark
    cv2.rectangle(diag, (crop_x, crop_y), (crop_x + crop_w, crop_y + crop_h), (34, 197, 94), 3)
    cv2.rectangle(diag, (sub_x, sub_y), (sub_x + sub_w, sub_y + sub_h), (0, 215, 255), 4)
    for fb in face_boxes:
        cv2.rectangle(diag, (fb["x"], fb["y"]), (fb["x"] + fb["w"], fb["y"] + fb["h"]), (255, 80, 200), 3)

    overlay_pil = build_prepared_overlay_png(
        frame_path=frame_path,
        target_w=out_w,
        target_h=out_h,
        box_x=box_x,
        box_y=box_y,
        box_w=box_w,
        box_h=box_h,
        border_radius=int(settings.get("border_radius", 24)),
        border_width=int(settings.get("border_width", 6)),
        border_color=str(settings.get("border_color", "#f5c542")),
        punch_hole=True,
    )

    cropped_video_part = mid_frame[sub_y:sub_y + sub_h, sub_x:sub_x + sub_w]
    if reframe_mode == "fit_contain":
        # Scale preserving exact aspect ratio inside (box_w, box_h) without stretching or cropping
        scale_factor = min(box_w / max(1.0, float(sub_w)), box_h / max(1.0, float(sub_h)))
        fit_w = max(2, int(round(sub_w * scale_factor)))
        fit_h = max(2, int(round(sub_h * scale_factor)))
        scaled_part = cv2.resize(cropped_video_part, (fit_w, fit_h), interpolation=cv2.INTER_AREA)
        resized_video_part = np.zeros((box_h, box_w, 3), dtype=np.uint8)
        off_x = (box_w - fit_w) // 2
        off_y = (box_h - fit_h) // 2
        resized_video_part[off_y:off_y + fit_h, off_x:off_x + fit_w] = scaled_part
    else:
        resized_video_part = cv2.resize(cropped_video_part, (box_w, box_h), interpolation=cv2.INTER_AREA)
    resized_rgb = cv2.cvtColor(resized_video_part, cv2.COLOR_BGR2RGB)
    video_pil = Image.fromarray(resized_rgb).convert("RGBA")

    canvas = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 255))
    canvas.paste(video_pil, (box_x, box_y))
    final_pil = Image.alpha_composite(canvas, overlay_pil).convert("RGB")

    diag_rgb = cv2.cvtColor(diag, cv2.COLOR_BGR2RGB)
    diag_pil = Image.fromarray(diag_rgb)
    diag_pil.thumbnail((540, 960), Image.Resampling.LANCZOS)
    final_pil_thumb = final_pil.copy()
    final_pil_thumb.thumbnail((540, 960), Image.Resampling.LANCZOS)

    def _to_b64(pil_img: Image.Image) -> str:
        import io
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    # Also compute the ideal box_h_pct that matches 100% of the source active video's aspect ratio
    src_active_aspect = crop_w / max(1.0, float(crop_h))
    ideal_box_h_px = box_w / src_active_aspect
    ideal_box_h_pct = round((ideal_box_h_px / out_h) * 100.0, 2)
    ideal_box_y_pct = round((box_y + (box_h - ideal_box_h_px) / 2.0) / out_h * 100.0, 2)

    return {
        "source_preview_b64": _to_b64(diag_pil),
        "output_preview_b64": _to_b64(final_pil_thumb),
        "detected": {
            "top_pct": top_pct,
            "bottom_pct": bottom_pct,
            "left_pct": left_pct,
            "right_pct": right_pct,
            "focus_x": round(focus_x, 3),
            "focus_y": round(focus_y, 3),
            "faces_detected": len(face_boxes),
            "src_crop": {"x": sub_x, "y": sub_y, "w": sub_w, "h": sub_h},
            "dst_box": {"x": box_x, "y": box_y, "w": box_w, "h": box_h},
            "canvas_w": out_w,
            "canvas_h": out_h,
            "ideal_box_h_pct": ideal_box_h_pct,
            "ideal_box_y_pct": ideal_box_y_pct,
            "video_meta": meta,
        },
    }


def process_single_video(
    video_path: str,
    frame_path: str,
    output_path: str,
    settings: Dict[str, Any],
    video_title: str = "",
) -> Dict[str, Any]:
    """
    Full AI Auto-Reframe + Frame Replacement + Batch Metadata Cleaning/Spoofing processor.
    """
    t0 = time.time()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    frames, meta = sample_video_frames(video_path, num_samples=12)
    vw, vh = meta["width"], meta["height"]

    if settings.get("auto_strip_old_frame", True):
        auto_crop = detect_old_frame_crop(video_path)
        top_pct = float(settings.get("override_top_pct") if settings.get("override_top_pct") is not None else auto_crop["top_pct"])
        bottom_pct = float(settings.get("override_bottom_pct") if settings.get("override_bottom_pct") is not None else auto_crop["bottom_pct"])
        left_pct = float(settings.get("override_left_pct") if settings.get("override_left_pct") is not None else auto_crop["left_pct"])
        right_pct = float(settings.get("override_right_pct") if settings.get("override_right_pct") is not None else auto_crop["right_pct"])
    else:
        top_pct = float(settings.get("override_top_pct") or 0.0)
        bottom_pct = float(settings.get("override_bottom_pct") or 0.0)
        left_pct = float(settings.get("override_left_pct") or 0.0)
        right_pct = float(settings.get("override_right_pct") or 0.0)

    crop_x = int(round(vw * (left_pct / 100.0)))
    crop_y = int(round(vh * (top_pct / 100.0)))
    crop_w = max(64, int(round(vw * (1.0 - (left_pct + right_pct) / 100.0))))
    crop_h = max(64, int(round(vh * (1.0 - (top_pct + bottom_pct) / 100.0))))

    reframe_mode = settings.get("reframe_mode", "preserve_full")
    zoom_pct = float(settings.get("zoom_pct") or 100.0)
    pan_x_pct = float(settings.get("pan_x_pct") or 0.0)
    pan_y_pct = float(settings.get("pan_y_pct") or 0.0)

    focus_x, focus_y, face_boxes = detect_ai_subject_center(frames, crop_x, crop_y, crop_w, crop_h)
    if settings.get("manual_focus_x") is not None:
        focus_x = float(settings["manual_focus_x"])
    if settings.get("manual_focus_y") is not None:
        focus_y = float(settings["manual_focus_y"])

    out_w, out_h = resolve_canvas_dimensions(frame_path, settings)

    box_x = int(round(out_w * (float(settings.get("box_x_pct", 0.0)) / 100.0)))
    box_y = int(round(out_h * (float(settings.get("box_y_pct", 33.7)) / 100.0)))
    box_w = max(64, int(round(out_w * (float(settings.get("box_w_pct", 100.0)) / 100.0))))
    box_h = max(64, int(round(out_h * (float(settings.get("box_h_pct", 31.0)) / 100.0))))
    box_w = box_w - (box_w % 2)
    box_h = box_h - (box_h % 2)

    sub_x, sub_y, sub_w, sub_h = compute_smart_reframe_box(
        crop_x, crop_y, crop_w, crop_h,
        box_w, box_h,
        focus_x_norm=focus_x,
        focus_y_norm=focus_y,
        reframe_mode=reframe_mode,
        zoom_pct=zoom_pct,
        pan_x_pct=pan_x_pct,
        pan_y_pct=pan_y_pct,
    )
    sub_w = max(32, sub_w - (sub_w % 2))
    sub_h = max(32, sub_h - (sub_h % 2))

    overlay_pil = build_prepared_overlay_png(
        frame_path=frame_path,
        target_w=out_w,
        target_h=out_h,
        box_x=box_x,
        box_y=box_y,
        box_w=box_w,
        box_h=box_h,
        border_radius=int(settings.get("border_radius", 24)),
        border_width=int(settings.get("border_width", 6)),
        border_color=str(settings.get("border_color", "#f5c542")),
        punch_hole=True,
    )

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_f:
        tmp_overlay_path = tmp_f.name
    overlay_pil.save(tmp_overlay_path, format="PNG")

    # Build Metadata cleaning & spoofing args
    metadata_args, applied_meta = build_metadata_ffmpeg_args(settings, video_title=video_title)

    # Optional subtle bitrate/hash randomization so every exported video has a unique binary signature
    unique_hash_mode = settings.get("metadata_unique_hash", True)
    audio_bitrate = f"{random.choice([160, 176, 192, 224])}k" if unique_hash_mode else "192k"
    crf_val = str(random.choice([19, 20, 21])) if unique_hash_mode else "20"

    try:
        ffmpeg_exe = get_ffmpeg_exe()
        # Subtle micro-eq saturation/contrast shift (imperceptible 0.2%) if unique_hash_mode is enabled
        eq_filter = ",eq=saturation=1.004:contrast=1.002" if unique_hash_mode else ""
        if reframe_mode == "fit_contain":
            scale_pad_part = (
                f"scale={box_w}:{box_h}:force_original_aspect_ratio=decrease:flags=lanczos{eq_filter},setsar=1,"
                f"pad={box_w}:{box_h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"pad={out_w}:{out_h}:{box_x}:{box_y}:black[bg];"
            )
        else:
            scale_pad_part = (
                f"scale={box_w}:{box_h}:flags=lanczos{eq_filter},setsar=1,"
                f"pad={out_w}:{out_h}:{box_x}:{box_y}:black[bg];"
            )
        filter_complex = (
            f"[0:v]crop={sub_w}:{sub_h}:{sub_x}:{sub_y},"
            f"{scale_pad_part}"
            f"[bg][1:v]overlay=0:0:format=auto,format=yuv420p,setsar=1[outv]"
        )

        cmd = [
            ffmpeg_exe,
            "-y",
            "-i", video_path,
            "-i", tmp_overlay_path,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "0:a?",
            "-aspect", f"{out_w}:{out_h}",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", crf_val,
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            *metadata_args,
            "-movflags", "+faststart+use_metadata_tags",
            output_path,
        ]

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg error: {proc.stderr[-600:]}")
    finally:
        if os.path.exists(tmp_overlay_path):
            try:
                os.remove(tmp_overlay_path)
            except OSError:
                pass

    elapsed = round(time.time() - t0, 2)
    return {
        "status": "success",
        "input_path": video_path,
        "output_path": output_path,
        "elapsed_sec": elapsed,
        "faces_detected": len(face_boxes),
        "crop_used": {"x": sub_x, "y": sub_y, "w": sub_w, "h": sub_h},
        "metadata_applied": applied_meta,
    }


def retag_existing_video_metadata(
    video_path: str,
    settings: Dict[str, Any],
    video_title: str = "",
) -> Dict[str, Any]:
    """
    Ultra-fast (0.1s/video) lossless Metadata Cleaner & Editor Spoofer for already-rendered
    videos in `Kho Video Hoàn Thành` without re-encoding video streams.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Không tìm thấy video: {video_path}")

    ffmpeg_exe = get_ffmpeg_exe()
    metadata_args, applied_meta = build_metadata_ffmpeg_args(settings, video_title=video_title)

    with tempfile.NamedTemporaryFile(suffix=".mp4", dir=os.path.dirname(video_path), delete=False) as tmp_f:
        tmp_out = tmp_f.name

    try:
        cmd = [
            ffmpeg_exe,
            "-y",
            "-i", video_path,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c", "copy",
            *metadata_args,
            "-movflags", "+faststart+use_metadata_tags",
            tmp_out,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg retag error: {proc.stderr[-500:]}")
        os.replace(tmp_out, video_path)
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass

    return {
        "status": "retagged",
        "video_path": video_path,
        "metadata_applied": applied_meta,
    }

