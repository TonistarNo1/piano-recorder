# piano-recorder (Docker-Stack)

Dieses Projekt stellt einen vollständigen Recorder-Stack bereit für:
- **Video:** Elgato Cam Link 4K (`/dev/video0`)
- **Audio:** Focusrite Scarlett 2i2 (`plughw:1`)
- **Output:** direkt auf externer SSD unter `/mnt/ssd`

## Schritt-für-Schritt Deployment auf Think Centre VM (Proxmox)

1. **Projekt in die VM kopieren**
   ```bash
   rsync -avz ./piano-recorder/ user@THINKCENTRE_VM:/opt/piano-recorder/
   ```

2. **In der VM auf den Stack-Ordner wechseln**
   ```bash
   cd /opt/piano-recorder
   ```

3. **SSD-Verzeichnisse auf dem Host anlegen**
   ```bash
   sudo mkdir -p /mnt/ssd/piano /mnt/ssd/archive /mnt/ssd/state /mnt/ssd/logs
   sudo chown -R $USER:$USER /mnt/ssd
   ```

4. **Docker Stack starten**
   ```bash
   docker compose up -d --build
   ```

5. **Recorder testen (HTTP API)**
   ```bash
   curl -X POST http://localhost:5051/start
   curl http://localhost:5051/status
   curl -X POST http://localhost:5051/stop
   ```

## Ergebnisstruktur auf der SSD

- Aufnahmen:
  - `/mnt/ssd/piano/YYYY/MM/DD/take_NNN__YYYY-MM-DD__HH-MM-SS/`
- Archiv:
  - `/mnt/ssd/archive/...`
- State:
  - `/mnt/ssd/state/next_take.txt`
- Logs:
  - `/mnt/ssd/logs/recorder_server.log`
  - `/mnt/ssd/logs/archive_worker.log`
  - `/mnt/ssd/piano/.../ffmpeg.log` (pro Take)

## Dateien pro Aufnahme

Jede Aufnahme erzeugt:
- `video.mp4` (Video + Stereo-Mix Audio)
- `audio_mix.wav` (gemischtes Stereo)
- `left.wav` (separate linke Spur)
- `right.wav` (separate rechte Spur)
- `thumbnail.jpg`
- `metadata.json`
- `ffmpeg.log`

## API Endpoints

- `POST /start` startet Aufnahme und liefert `recording_id`, `take_number`, Status
- `POST /stop` stoppt Aufnahme, finalisiert Dateien + Metadaten
- `GET /status` liefert aktuellen Status oder letzte Aufnahme

## Archivierung / Cleanup

Der `archiver`-Service läuft täglich und verschiebt Takes, deren Datum älter als **14 Tage** ist, von:
- `/mnt/ssd/piano/...`

nach:
- `/mnt/ssd/archive/...`

Anschließend werden leere Quellordner entfernt.

## Quick Sync Hinweis

Wenn Intel iGPU in der VM durchgereicht ist, nutzt der Recorder automatisch `h264_qsv`.
Falls `h264_qsv` nicht verfügbar ist, wird automatisch auf `libx264` zurückgefallen.

Falls `/dev/dri` in der VM nicht existiert, entferne in `docker-compose.yml` die Zeile:
```yaml
- /dev/dri:/dev/dri
```
