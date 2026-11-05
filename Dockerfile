FROM python:3.11-slim

# Cài đặt FFmpeg và các thư viện hệ thống cần thiết cho OpenCV & xử lý video
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài đặt thư viện Python
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ mã nguồn vào container
COPY . .

# Tạo sẵn các thư mục dữ liệu mặc định và khung mẫu
RUN mkdir -p data/kho_video_mau data/khung data/kho_video_hoan_thanh && \
    python init_demo_assets.py || true

# Hỗ trợ cổng động $PORT của Render.com / Railway / Koyeb (mặc định 8000)
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
