# Schritt 1: Python-Base-Image mit Debian nutzen (stabil für ffmpeg + ALSA + v4l2)
FROM python:3.11-slim

# Schritt 2: Systemabhängigkeiten installieren
# - ffmpeg: Aufnahme/Transcoding/Thumbnail
# - v4l-utils/alsa-utils: Video-/Audio-Device-Tools
# - tini: saubere Signalweitergabe (wichtig für Stop von ffmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    v4l-utils \
    alsa-utils \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Schritt 3: Arbeitsverzeichnis setzen
WORKDIR /app

# Schritt 4: Python-Abhängigkeiten installieren
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Schritt 5: Applikationscode kopieren
COPY server.py /app/server.py
COPY archive_worker.py /app/archive_worker.py

# Schritt 6: Recorder-Port dokumentieren
EXPOSE 5051

# Schritt 7: tini als EntryPoint für stabile Prozessführung nutzen
ENTRYPOINT ["/usr/bin/tini", "--"]

# Standardkommando: Recorder-Server starten
CMD ["python", "/app/server.py"]
