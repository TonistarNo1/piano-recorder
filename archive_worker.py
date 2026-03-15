#!/usr/bin/env python3
"""Täglicher Archivierungs-Worker für Aufnahmen älter als X Tage."""

import logging
import os
import shutil
import signal
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Tuple

# Schritt 1: Konfiguration laden
RECORD_ROOT = Path(os.getenv("RECORD_ROOT", "/mnt/ssd/piano"))
ARCHIVE_ROOT = Path(os.getenv("ARCHIVE_ROOT", "/mnt/ssd/archive"))
LOG_ROOT = Path(os.getenv("LOG_ROOT", "/mnt/ssd/logs"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "14"))
ARCHIVE_CHECK_SECONDS = int(os.getenv("ARCHIVE_CHECK_SECONDS", "86400"))

RUNNING = True


def setup_logging() -> logging.Logger:
    """Schritt 2: Logging für den Worker auf SSD initialisieren."""
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_file = LOG_ROOT / "archive_worker.log"

    logger = logging.getLogger("archive_worker")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.info("Archive-Worker Logging initialisiert: %s", log_file)
    return logger


LOGGER = setup_logging()


def stop_signal_handler(signum, _frame) -> None:  # noqa: ANN001
    """Schritt 3: Sauberes Beenden bei Container-Stop."""
    global RUNNING
    LOGGER.info("Signal %s empfangen, Worker wird beendet", signum)
    RUNNING = False


signal.signal(signal.SIGTERM, stop_signal_handler)
signal.signal(signal.SIGINT, stop_signal_handler)


def ensure_layout() -> None:
    """Schritt 4: Sicherstellen, dass Zielordner existieren und beschreibbar sind."""
    RECORD_ROOT.mkdir(parents=True, exist_ok=True)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)

    write_test = ARCHIVE_ROOT / ".archive_write_test"
    write_test.write_text("ok", encoding="utf-8")
    write_test.unlink(missing_ok=True)


def iter_take_directories(root: Path) -> Iterable[Path]:
    """Alle Take-Ordner im Schema /YYYY/MM/DD/take_* liefern."""
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*/*/*/take_*") if path.is_dir())


def parse_date_from_take_path(take_dir: Path) -> Optional[Tuple[int, int, int]]:
    """Schritt 5: Aufnahmedatum aus Ordnerstruktur lesen."""
    try:
        relative = take_dir.relative_to(RECORD_ROOT)
        year, month, day = relative.parts[0], relative.parts[1], relative.parts[2]
        return int(year), int(month), int(day)
    except Exception:  # noqa: BLE001
        return None


def cleanup_empty_parent_dirs(path: Path, stop_root: Path) -> None:
    """Leere Tages/Monats/Jahres-Ordner nach Move entfernen."""
    current = path.parent
    while current != stop_root and current.is_dir():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def archive_old_recordings() -> dict[str, int]:
    """Schritt 6: Aufnahmen älter als RETENTION_DAYS ins Archiv verschieben."""
    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    moved = 0
    skipped = 0

    for take_dir in iter_take_directories(RECORD_ROOT):
        parsed = parse_date_from_take_path(take_dir)
        if parsed is None:
            LOGGER.warning("Kann Datum nicht parsen, überspringe: %s", take_dir)
            skipped += 1
            continue

        take_date = date(parsed[0], parsed[1], parsed[2])
        if take_date >= cutoff:
            continue

        relative = take_dir.relative_to(RECORD_ROOT)
        target = ARCHIVE_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = target.parent / f"{take_dir.name}__archived_{suffix}"

        LOGGER.info("Archivierung: %s -> %s", take_dir, target)
        shutil.move(str(take_dir), str(target))
        cleanup_empty_parent_dirs(take_dir, RECORD_ROOT)
        moved += 1

    LOGGER.info(
        "Archivierungszyklus fertig | cutoff=%s | moved=%s | skipped=%s",
        cutoff.isoformat(),
        moved,
        skipped,
    )

    return {"moved": moved, "skipped": skipped}


def main() -> None:
    """Schritt 7: Endlosschleife mit täglichem Archivierungsintervall."""
    ensure_layout()
    LOGGER.info("Archive-Worker gestartet (retention_days=%s)", RETENTION_DAYS)

    while RUNNING:
        try:
            archive_old_recordings()
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Archivierungszyklus fehlgeschlagen: %s", exc)

        # In kleineren Schritten schlafen, damit SIGTERM zügig greift.
        slept = 0
        while RUNNING and slept < ARCHIVE_CHECK_SECONDS:
            time.sleep(1)
            slept += 1

    LOGGER.info("Archive-Worker beendet")


if __name__ == "__main__":
    main()
