FROM python:3.12-slim
# ffprobe checks each channel's stream once a day; ffmpeg draws its poster.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /data
# A new config listens where the published port reaches it.
ENV REX_LIVE_SPORTS_LISTEN=0.0.0.0:9983
COPY rex_live_sports.py /app/rex_live_sports.py
EXPOSE 9983
CMD ["python3", "/app/rex_live_sports.py", "--config", "/data/config.json"]
