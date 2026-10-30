"""The nightbank night's properties and profile, built the way the retired sandbox launcher built
them, for the tests that run nightbank's estate: the per-region end-of-day times staggered from
one UTC anchor, the heartbeat window, and the night's directories and profile.env."""

from __future__ import annotations

import math
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent / "examples" / "nightbank"
REGION_TZ = {  # anchor order is the follow-the-sun order
    "APAC": "Asia/Tokyo",
    "EMEA": "Europe/Zurich",
    "AMER": "America/New_York",
}


def ceil_minute(t: datetime) -> datetime:
    return (
        (t + timedelta(seconds=math.ceil(t.second / 60) * 60 - t.second)).replace(
            second=0, microsecond=0
        )
        if t.second or t.microsecond
        else t
    )


def compute_properties(run_dir: Path, anchor_utc: datetime, stagger_mins: int) -> dict[str, str]:
    props = {
        "OWNER": os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown",
        "NB_DATA": str(run_dir / "data"),
        "NB_LOGS": str(run_dir / "logs"),
        "NB_PROFILE": str(run_dir / "profile.env"),
    }
    anchor = ceil_minute(anchor_utc)
    for i, (region, tz) in enumerate(REGION_TZ.items()):
        local = (anchor + timedelta(minutes=i * stagger_mins)).astimezone(ZoneInfo(tz))
        props[f"EOD_{region}"] = local.strftime("%H:%M")
    win_start = anchor_utc.replace(second=0, microsecond=0)
    win_end = min(win_start + timedelta(minutes=90), win_start.replace(hour=23, minute=59))
    props["HB_WINDOW"] = f"{win_start.strftime('%H:%M')}-{win_end.strftime('%H:%M')}"
    return props


def write_kv(path: Path, kv: dict[str, str]) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))


def prepare_night(
    run_dir: Path,
    estate: str,
    *,
    anchor_utc: datetime,
    stagger_mins: int = 3,
    incidents: bool = True,
) -> dict[str, str]:
    """The night's directories, its properties file and its profile."""
    for sub in ("data/incoming", "data/pending", "data/current", "data/state", "logs"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)  # run artifacts are owner-only (journal/logs/data)

    props = compute_properties(run_dir, anchor_utc, stagger_mins)
    write_kv(run_dir / "night.properties", props)
    os.chmod(run_dir / "night.properties", 0o600)

    env_lines = {
        "NIGHTBANK_DATA": props["NB_DATA"],
        "NIGHTBANK_STATE": f"{props['NB_DATA']}/state",
        "NIGHTBANK_SOCKET": str(run_dir / "engine" / "control.sock"),
        "PATH": f"{BASE / 'bin'}:{Path(sys.executable).parent}:$PATH",
    }
    if incidents:
        # per-estate incidents: the bank estate's targets are per-asset-class
        # names that only exist there (each estate ships its own file)
        shutil.copy(BASE / "estate" / estate / "incidents.conf", run_dir / "incidents.conf")
        env_lines["NIGHTBANK_INCIDENTS"] = str(run_dir / "incidents.conf")
    (run_dir / "profile.env").write_text(
        "".join(f'export {k}="{v}"\n' for k, v in env_lines.items())
    )
    os.chmod(run_dir / "profile.env", 0o600)
    return props
