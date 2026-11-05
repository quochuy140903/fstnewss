import os
import cv2
import numpy as np
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
INPUT_DIR = os.path.join(DATA_DIR, "kho_video_mau")
FRAMES_DIR = os.path.join(DATA_DIR, "khung")
OUTPUT_DIR = os.path.join(DATA_DIR, "kho_video_hoan_thanh")

USER_IMG1 = r"C:/Users/Admin/.gemini/antigravity/brain/ed67b624-3aa6-49a7-bd9a-897862333802/.user_uploaded/media_1793846273932_a811abf6.jpg"
USER_IMG2 = r"C:/Users/Admin/.gemini/antigravity/brain/ed67b624-3aa6-49a7-bd9a-897862333802/.user_uploaded/media_1793846286219_5b0c2c76.jpg"


def prepare_assets():
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(FRAMES_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Prepare Frame 2 (SNL Comedy Classics 9:16 template without outer white margins or player bar)
    if os.path.exists(USER_IMG2):
        img2 = Image.open(USER_IMG2).convert("RGB")
        arr2 = np.array(img2)
        gray = cv2.cvtColor(arr2, cv2.COLOR_RGB2GRAY)
        non_white = gray < 240
        cols = np.where(np.mean(non_white, axis=0) > 0.50)[0]
        rows = np.where(np.mean(non_white[:, cols[0]:cols[-1]], axis=1) > 0.50)[0]
        if len(cols) > 50 and len(rows) > 50:
            x1, x2 = int(cols[0]), int(cols[-1])
            y1, y2 = int(rows[0]), int(rows[-1])
            cropped_frame = img2.crop((x1, y1, x2, y2)).resize((1080, 1920), Image.Resampling.LANCZOS)
        else:
            cropped_frame = img2.resize((1080, 1920), Image.Resampling.LANCZOS)

        frame_out_path = os.path.join(FRAMES_DIR, "khung_snl_comedy_classics.png")
        cropped_frame.save(frame_out_path, format="PNG")
        print(f"Saved frame template: {frame_out_path}")

    # 2. Prepare Sample Video 1 from Image 1
    if os.path.exists(USER_IMG1):
        img1 = Image.open(USER_IMG1).convert("RGB").resize((1080, 1920), Image.Resampling.LANCZOS)
        base_bgr = cv2.cvtColor(np.array(img1), cv2.COLOR_RGB2BGR)

        y_top, y_bot = 528, 1411
        inner_clip = base_bgr[y_top:y_bot, 32:1080].copy()
        ih, iw = inner_clip.shape[:2]

        video_out_path = os.path.join(INPUT_DIR, "video_mau_snl_demo.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(video_out_path, fourcc, 24.0, (1080, 1920))

        for i in range(72):
            frame = base_bgr.copy()
            dx = int(round(12 * np.sin(i * 0.18)))
            dy = int(round(6 * np.cos(i * 0.18)))
            M = np.float32([[1.03, 0, dx - 16], [0, 1.03, dy - 12]])
            moved_inner = cv2.warpAffine(inner_clip, M, (iw, ih), borderMode=cv2.BORDER_REFLECT)
            frame[y_top:y_bot, 32:1080] = moved_inner
            writer.write(frame)

        writer.release()
        print(f"Saved sample video: {video_out_path}")


if __name__ == "__main__":
    prepare_assets()
