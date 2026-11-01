"""Constants for Prusa G-code Viewer."""

from __future__ import annotations

DOMAIN = "prusa_gcode_viewer"
PRUSALINK_DOMAIN = "prusalink"

CONF_PRINTER = "printer"  # the PrusaLink config entry this viewer reads from

URL_BASE = "/prusa_gcode_viewer"
CARD_FILE = "prusa-gcode-viewer-card.js"
MODEL_URL = "/api/prusa_gcode_viewer/model/{entry_id}/{key}"

# Built models kept on disk; older ones are deleted.
CACHE_KEEP = 5
# Largest G-code file downloaded from the printer.
MAX_FILE_SIZE = 256 * 1024 * 1024
JOB_TIMEOUT = 10
# The printer serves files slowly while it prints (about 10 kB/s on a CORE One), so a
# large file can take most of an hour; give up only after a long silence or 3 hours.
DOWNLOAD_STALL_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 3 * 60 * 60
# A failed build is retried after this many seconds.
RETRY_AFTER = 120

# PrusaLink sensor keys (unique_id suffixes) the card follows.
PRUSALINK_SENSORS = {
    "state": "printer.state",
    "z": "printer.telemetry.z-height",
    "progress": "job.progress",
    "file": "job.filename",
}
