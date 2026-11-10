"""The device's battery level, as it reports it with each image request.

The device sends its battery voltage (`?battery_mv=3950`), not a percentage,
so the conversion lives here and can be tuned without reflashing.

Calibration: a full battery reads below the curve's 4.20 V on a real device
(the cell settles after charging, and the ESP32's ADC through a halving
divider reads a few tens of mV low), so `full_mv` (config [battery]) is what
this device reads when full; readings are shifted by the difference.

Every report is appended to a CSV log (time, mV) to learn the real discharge
curve and battery life; the last line also restores the level after a restart.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Typical single-cell LiPo discharge curve under light load: (mV, %).
# Rough by nature: the voltage sags while Wi-Fi is on and recovers after.
_CURVE = [
    (4200, 100), (4150, 95), (4110, 90), (4080, 85), (4020, 80), (3980, 75),
    (3950, 70), (3910, 65), (3870, 60), (3850, 55), (3840, 50), (3820, 45),
    (3800, 40), (3790, 35), (3770, 30), (3750, 25), (3730, 20), (3710, 15),
    (3690, 10), (3610, 5), (3270, 0),
]

LOW_PERCENT = 15  # at or below: warning badge in the status bar
PLAUSIBLE_MV = range(2500, 5001)  # anything else is a bad reading
MAX_AGE = timedelta(hours=2)  # older reports aren't shown
LOG_MIN_INTERVAL = timedelta(seconds=30)  # between lines in the CSV log


CURVE_FULL_MV = _CURVE[0][0]


def percent(mv: int, full_mv: int = CURVE_FULL_MV) -> int:
    """Battery percentage for a voltage, interpolated on the LiPo curve.

    full_mv: what this device reads for a full battery; the reading is shifted
    so that it maps to the curve's 100 %.
    """
    mv += CURVE_FULL_MV - full_mv
    if mv >= _CURVE[0][0]:
        return 100
    for (hi_mv, hi_pct), (lo_mv, lo_pct) in zip(_CURVE, _CURVE[1:]):
        if mv >= lo_mv:
            return round(lo_pct + (hi_pct - lo_pct) * (mv - lo_mv) / (hi_mv - lo_mv))
    return 0


class Battery:
    """The latest report from the device (thread-safe), optionally logged to CSV."""

    def __init__(self, full_mv: int = CURVE_FULL_MV, log_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._report: tuple[int, datetime] | None = None
        self._logged_at: datetime | None = None
        self.full_mv = full_mv
        self.log_path = log_path
        if log_path:
            self._report = _last_logged(log_path)

    def report(self, mv_text: str | None, at: datetime) -> None:
        """Record a `battery_mv` query value; ignores missing or implausible ones."""
        try:
            mv = int(mv_text or "")
        except ValueError:
            return
        if mv in PLAUSIBLE_MV:
            with self._lock:
                self._report = (mv, at)
                # The device reports at most once a minute (plus button
                # presses). Anything faster isn't the device, and must not be
                # able to fill the disk: such reports aren't logged.
                if self._logged_at and abs(at - self._logged_at) < LOG_MIN_INTERVAL:
                    return
                self._logged_at = at
                if self.log_path:
                    try:
                        self.log_path.parent.mkdir(parents=True, exist_ok=True)
                        new = not self.log_path.exists()
                        with open(self.log_path, "a") as f:
                            if new:
                                f.write("time,mv\n")
                            f.write(f"{at.isoformat(timespec='seconds')},{mv}\n")
                    except OSError as e:
                        log.warning("could not log battery reading: %s", e)

    def level(self, now: datetime) -> int | None:
        """Percentage from a recent report, or None if there is none."""
        with self._lock:
            report = self._report
        if report is None or now - report[1] > MAX_AGE:
            return None
        return percent(report[0], self.full_mv)


def _last_logged(path: Path) -> tuple[int, datetime] | None:
    """The last (mV, time) in the log, if any."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200))
            last = f.read().decode("utf-8", "replace").strip().splitlines()[-1]
        time_text, mv_text = last.split(",")
        return int(mv_text), datetime.fromisoformat(time_text)
    except (OSError, ValueError, IndexError):
        return None
