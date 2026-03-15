#!/usr/bin/env python3
"""HTTP Recorder-Server für Video + Audio Capture auf externer SSD."""

import fcntl
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

from flask import Flask, jsonify

# Schritt 1: Konfiguration über Environment-Variablen laden
RECORD_ROOT = Path(os.getenv("RECORD_ROOT", "/mnt/ssd/piano"))
ARCHIVE_ROOT = Path(os.getenv("ARCHIVE_ROOT", "/mnt/ssd/archive"))
STATE_ROOT = Path(os.getenv("STATE_ROOT", "/mnt/ssd/state"))
LOG_ROOT = Path(os.getenv("LOG_ROOT", "/mnt/ssd/logs"))
VIDEO_DEVICE = os.getenv("VIDEO_DEVICE", "/dev/video0")
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "plughw:1")
VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "1280"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "720"))
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "30"))
AUDIO_RATE = int(os.getenv("AUDIO_RATE", "48000"))
ENCODER_PREFERENCE = os.getenv("ENCODER_PREFERENCE", "qsv_auto")
STOP_TIMEOUT_SECONDS = int(os.getenv("STOP_TIMEOUT_SECONDS", "20"))

# Schritt 2: Globale Zustandsvariablen für laufende Aufnahme
STATE_LOCK = threading.Lock()
CURRENT_RECORDING: Optional[Dict] = None
LAST_RECORDING: Optional[Dict] = None
FFMPEG_PROCESS: Optional[subprocess.Popen] = None
FFMPEG_LOG_HANDLE = None


def now_iso() -> str:
    """Zeitstempel im ISO-Format (lokale Zeitzone)."""
    return datetime.now().astimezone().isoformat()


def setup_logging() -> logging.Logger:
    """Schritt 3: Logging in Datei auf SSD + stdout einrichten."""
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_file = LOG_ROOT / "recorder_server.log"

    logger = logging.getLogger("piano_recorder")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.info("Recorder-Logging initialisiert: %s", log_file)
    return logger


LOGGER = setup_logging()


def ensure_storage_layout() -> None:
    """Schritt 4: SSD-Verzeichnisstruktur anlegen und Schreibrechte prüfen."""
    for base in [RECORD_ROOT, ARCHIVE_ROOT, STATE_ROOT, LOG_ROOT]:
        base.mkdir(parents=True, exist_ok=True)

    write_test = STATE_ROOT / ".write_test"
    write_test.write_text("ok", encoding="utf-8")
    write_test.unlink(missing_ok=True)

    next_take = STATE_ROOT / "next_take.txt"
    if not next_take.exists():
        next_take.write_text("1\n", encoding="utf-8")

    LOGGER.info("SSD-Pfade bereit und beschreibbar unter /mnt/ssd")


ensure_storage_layout()


@lru_cache(maxsize=1)
def ffmpeg_encoders_text() -> str:
    """Schritt 5: verfügbare ffmpeg-Encoder einmalig ermitteln."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "") + (result.stderr or "")


def select_video_encoder() -> Tuple[str, list]:
    """Encoder wählen: bevorzugt h264_qsv, sonst libx264 als Fallback."""
    if ENCODER_PREFERENCE in {"qsv_auto", "h264_qsv"}:
        has_dri = Path("/dev/dri").exists()
        has_qsv_encoder = "h264_qsv" in ffmpeg_encoders_text()
        if has_dri and has_qsv_encoder:
            LOGGER.info("Intel Quick Sync aktiv: h264_qsv")
            return "h264_qsv", ["-global_quality", "24", "-look_ahead", "0"]

        LOGGER.warning(
            "Quick Sync nicht verfügbar (has_dri=%s, has_qsv_encoder=%s) -> Fallback libx264",
            has_dri,
            has_qsv_encoder,
        )

    return "libx264", ["-preset", "veryfast", "-crf", "23", "-tune", "zerolatency"]


def reserve_next_take_number() -> int:
    """Schritt 6: next_take.txt atomar lesen/erhöhen."""
    take_file = STATE_ROOT / "next_take.txt"

    with take_file.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        raw_value = handle.read().strip()

        current_take = int(raw_value) if raw_value.isdigit() else 1
        next_take = current_take + 1

        handle.seek(0)
        handle.truncate()
        handle.write(f"{next_take}\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    LOGGER.info("Take-Nummer reserviert: %s", current_take)
    return current_take


def make_recording_dir(take_number: int, started_at: datetime) -> Path:
    """Schritt 7: Zielordner im geforderten Format erzeugen."""
    day_dir = RECORD_ROOT / started_at.strftime("%Y") / started_at.strftime("%m") / started_at.strftime("%d")
    take_name = f"take_{take_number:03d}__{started_at.strftime('%Y-%m-%d')}__{started_at.strftime('%H-%M-%S')}"
    base_dir = day_dir / take_name
    candidate = base_dir
    suffix = 1

    # Falls durch Clock/Retry doch eine Kollision entsteht, eindeutigen Suffix ergänzen.
    while candidate.exists():
        candidate = day_dir / f"{take_name}__retry_{suffix:02d}"
        suffix += 1

    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def build_ffmpeg_command(recording_dir: Path) -> Tuple[list, Dict[str, str], str]:
    """Schritt 8: ffmpeg-Kommando für Video + Mix + Left/Right bauen."""
    video_path = recording_dir / "video.mp4"
    audio_mix_path = recording_dir / "audio_mix.wav"
    left_path = recording_dir / "left.wav"
    right_path = recording_dir / "right.wav"
    thumbnail_path = recording_dir / "thumbnail.jpg"
    metadata_path = recording_dir / "metadata.json"
    ffmpeg_log_path = recording_dir / "ffmpeg.log"

    mix_expr = "0.5*c0+0.5*c1"
    filter_complex = (
        "[1:a]asplit=4[a_mix_video_src][a_mix_wav_src][a_left_src][a_right_src];"
        f"[a_mix_video_src]pan=stereo|c0={mix_expr}|c1={mix_expr}[a_mix_video];"
        f"[a_mix_wav_src]pan=stereo|c0={mix_expr}|c1={mix_expr}[a_mix_wav];"
        "[a_left_src]pan=mono|c0=c0[a_left];"
        "[a_right_src]pan=mono|c0=c1[a_right]"
    )

    video_encoder, video_encoder_args = select_video_encoder()

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-y",
        "-f",
        "v4l2",
        "-thread_queue_size",
        "2048",
        "-framerate",
        str(VIDEO_FPS),
        "-video_size",
        f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "-i",
        VIDEO_DEVICE,
        "-f",
        "alsa",
        "-thread_queue_size",
        "2048",
        "-ac",
        "2",
        "-ar",
        str(AUDIO_RATE),
        "-i",
        AUDIO_DEVICE,
        "-filter_complex",
        filter_complex,
        "-map",
        "0:v:0",
        "-map",
        "[a_mix_video]",
        "-c:v",
        video_encoder,
        *video_encoder_args,
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(VIDEO_FPS),
        "-g",
        str(VIDEO_FPS * 2),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(video_path),
        "-map",
        "[a_mix_wav]",
        "-c:a",
        "pcm_s32le",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "2",
        str(audio_mix_path),
        "-map",
        "[a_left]",
        "-c:a",
        "pcm_s32le",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "1",
        str(left_path),
        "-map",
        "[a_right]",
        "-c:a",
        "pcm_s32le",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "1",
        str(right_path),
    ]

    file_paths = {
        "video": str(video_path),
        "audio_mix": str(audio_mix_path),
        "left": str(left_path),
        "right": str(right_path),
        "thumbnail": str(thumbnail_path),
        "metadata": str(metadata_path),
        "ffmpeg_log": str(ffmpeg_log_path),
    }

    return command, file_paths, video_encoder


def file_info(path: str) -> Dict:
    """Datei-Metadaten für JSON-Ausgabe sammeln."""
    file_path = Path(path)
    exists = file_path.exists()
    return {
        "path": str(file_path),
        "exists": exists,
        "size_bytes": file_path.stat().st_size if exists else 0,
    }


def generate_thumbnail(video_path: str, thumbnail_path: str) -> bool:
    """Schritt 9: Thumbnail aus der MP4 erzeugen."""
    if not Path(video_path).exists():
        LOGGER.warning("Thumbnail übersprungen: Video fehlt (%s)", video_path)
        return False

    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            thumbnail_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        LOGGER.warning("Thumbnail-Erzeugung fehlgeschlagen: %s", result.stderr.strip())
        return False

    return Path(thumbnail_path).exists()


def finalize_recording_locked(final_status: str, return_code: Optional[int], reason: str) -> Dict:
    """Schritt 10: Aufnahme finalisieren und metadata.json schreiben (unter Lock)."""
    global CURRENT_RECORDING, LAST_RECORDING, FFMPEG_PROCESS, FFMPEG_LOG_HANDLE

    if CURRENT_RECORDING is None:
        return {"status": "idle", "message": "no active recording"}

    ended_at = now_iso()

    if FFMPEG_LOG_HANDLE is not None:
        try:
            FFMPEG_LOG_HANDLE.flush()
            FFMPEG_LOG_HANDLE.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Konnte ffmpeg-Loghandle nicht sauber schließen: %s", exc)
        FFMPEG_LOG_HANDLE = None

    thumbnail_created = generate_thumbnail(
        CURRENT_RECORDING["files"]["video"],
        CURRENT_RECORDING["files"]["thumbnail"],
    )

    started_dt = datetime.fromisoformat(CURRENT_RECORDING["started_at"])
    ended_dt = datetime.fromisoformat(ended_at)
    duration_seconds = max(0.0, (ended_dt - started_dt).total_seconds())

    metadata = {
        "recording_id": CURRENT_RECORDING["recording_id"],
        "status": final_status,
        "reason": reason,
        "take_number": CURRENT_RECORDING["take_number"],
        "recording_dir": CURRENT_RECORDING["recording_dir"],
        "started_at": CURRENT_RECORDING["started_at"],
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "encoder": CURRENT_RECORDING["video_encoder"],
        "ffmpeg_pid": CURRENT_RECORDING["ffmpeg_pid"],
        "ffmpeg_return_code": return_code,
        "thumbnail_created": thumbnail_created,
        "files": {
            "video": file_info(CURRENT_RECORDING["files"]["video"]),
            "audio_mix": file_info(CURRENT_RECORDING["files"]["audio_mix"]),
            "left": file_info(CURRENT_RECORDING["files"]["left"]),
            "right": file_info(CURRENT_RECORDING["files"]["right"]),
            "thumbnail": file_info(CURRENT_RECORDING["files"]["thumbnail"]),
            "ffmpeg_log": file_info(CURRENT_RECORDING["files"]["ffmpeg_log"]),
        },
    }

    metadata_path = Path(CURRENT_RECORDING["files"]["metadata"])
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    summary = {
        "status": final_status,
        "recording_id": CURRENT_RECORDING["recording_id"],
        "take_number": CURRENT_RECORDING["take_number"],
        "recording_dir": CURRENT_RECORDING["recording_dir"],
        "metadata_path": str(metadata_path),
        "ffmpeg_return_code": return_code,
        "ended_at": ended_at,
    }

    LAST_RECORDING = summary
    CURRENT_RECORDING = None
    FFMPEG_PROCESS = None

    LOGGER.info("Aufnahme finalisiert (%s): %s", final_status, summary)
    return summary


def refresh_state_locked() -> None:
    """Falls ffmpeg unerwartet beendet wurde, Status konsistent halten."""
    if CURRENT_RECORDING and FFMPEG_PROCESS and FFMPEG_PROCESS.poll() is not None:
        rc = FFMPEG_PROCESS.returncode
        status = "stopped" if rc == 0 else "failed"
        finalize_recording_locked(status, rc, "ffmpeg_process_exited")


app = Flask(__name__)


@app.route("/start", methods=["POST"])
def start_recording():
    """Startet eine neue Aufnahme."""
    global CURRENT_RECORDING, FFMPEG_PROCESS, FFMPEG_LOG_HANDLE

    with STATE_LOCK:
        refresh_state_locked()

        if CURRENT_RECORDING is not None:
            return (
                jsonify(
                    {
                        "status": "recording",
                        "message": "recording already running",
                        "recording_id": CURRENT_RECORDING["recording_id"],
                    }
                ),
                409,
            )

        ensure_storage_layout()

        recording_id = uuid.uuid4().hex[:12]
        take_number = reserve_next_take_number()
        started_at_dt = datetime.now().astimezone()
        recording_dir = make_recording_dir(take_number, started_at_dt)

        ffmpeg_cmd, file_paths, video_encoder = build_ffmpeg_command(recording_dir)
        ffmpeg_log_path = file_paths["ffmpeg_log"]
        ffmpeg_log_handle = open(ffmpeg_log_path, "a", encoding="utf-8")

        LOGGER.info("Starte Aufnahme %s (take=%s)", recording_id, take_number)
        LOGGER.info("ffmpeg command: %s", " ".join(ffmpeg_cmd))

        try:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=ffmpeg_log_handle,
                text=True,
            )
        except Exception as exc:  # noqa: BLE001
            ffmpeg_log_handle.close()
            LOGGER.exception("ffmpeg konnte nicht gestartet werden: %s", exc)
            return (
                jsonify(
                    {
                        "status": "failed",
                        "message": "failed to spawn ffmpeg process",
                        "error": str(exc),
                        "recording_id": recording_id,
                        "take_number": take_number,
                        "recording_dir": str(recording_dir),
                    }
                ),
                500,
            )

        CURRENT_RECORDING = {
            "recording_id": recording_id,
            "take_number": take_number,
            "recording_dir": str(recording_dir),
            "started_at": started_at_dt.isoformat(),
            "video_encoder": video_encoder,
            "ffmpeg_pid": process.pid,
            "files": file_paths,
        }
        FFMPEG_PROCESS = process
        FFMPEG_LOG_HANDLE = ffmpeg_log_handle

    # Kurzer Health-Check außerhalb des Locks, um Immediate-Fail früh zu erkennen.
    time.sleep(1.0)

    with STATE_LOCK:
        refresh_state_locked()
        if CURRENT_RECORDING is None:
            return (
                jsonify(
                    {
                        "status": "failed",
                        "message": "ffmpeg exited right after start, see ffmpeg.log",
                        "last_recording": LAST_RECORDING,
                    }
                ),
                500,
            )

        return jsonify(
            {
                "status": "recording",
                "recording_id": CURRENT_RECORDING["recording_id"],
                "take_number": CURRENT_RECORDING["take_number"],
                "recording_dir": CURRENT_RECORDING["recording_dir"],
                "started_at": CURRENT_RECORDING["started_at"],
                "video_encoder": CURRENT_RECORDING["video_encoder"],
            }
        )


@app.route("/stop", methods=["POST"])
def stop_recording():
    """Stoppt die laufende Aufnahme."""
    global FFMPEG_PROCESS

    with STATE_LOCK:
        refresh_state_locked()

        if CURRENT_RECORDING is None or FFMPEG_PROCESS is None:
            return jsonify({"status": "idle", "message": "no active recording"}), 409

        process = FFMPEG_PROCESS

        if process.poll() is None and process.stdin is not None:
            try:
                # ffmpeg sauber stoppen: 'q' auf stdin
                process.stdin.write("q\n")
                process.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Graceful stop via stdin fehlgeschlagen: %s", exc)

    # Warten außerhalb Lock, damit Status-Endpoint nicht komplett blockiert.
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        LOGGER.warning("ffmpeg stop timeout -> terminate")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            LOGGER.error("ffmpeg reagiert nicht -> kill")
            process.kill()
            process.wait(timeout=5)

    with STATE_LOCK:
        rc = process.returncode
        final_status = "stopped" if rc == 0 else "failed"
        summary = finalize_recording_locked(final_status, rc, "api_stop")

    return jsonify(summary)


@app.route("/status", methods=["GET"])
def status():
    """Liefert aktuellen Recorder-Status."""
    with STATE_LOCK:
        refresh_state_locked()

        if CURRENT_RECORDING is not None:
            return jsonify(
                {
                    "status": "recording",
                    "recording_id": CURRENT_RECORDING["recording_id"],
                    "take_number": CURRENT_RECORDING["take_number"],
                    "recording_dir": CURRENT_RECORDING["recording_dir"],
                    "started_at": CURRENT_RECORDING["started_at"],
                    "video_encoder": CURRENT_RECORDING["video_encoder"],
                }
            )

        return jsonify(
            {
                "status": "idle",
                "last_recording": LAST_RECORDING,
                "paths": {
                    "record_root": str(RECORD_ROOT),
                    "archive_root": str(ARCHIVE_ROOT),
                    "state_root": str(STATE_ROOT),
                    "log_root": str(LOG_ROOT),
                },
            }
        )


if __name__ == "__main__":
    LOGGER.info("Recorder-Server startet auf 0.0.0.0:5051")
    app.run(host="0.0.0.0", port=5051, debug=False, threaded=True)
