#!/usr/bin/env python3
"""
Script name: myWhoosh2Garmin.py
Usage: python myWhoosh2Garmin.py
Description:
    1. Scans for MyWhoosh workout .fit files.
    2. Performs device spoofing so Garmin Connect recognizes the device as a 'Garmin Edge 1030 Plus'.
    3. Calculates average cadence, power, heart rate, Normalized Power (NP®), Intensity Factor (IF®),
       Training Stress Score (TSS®), and total work (kJ), and strips unwanted fields.
    4. Supports multi-user profile filtering (via MYWHOOSH_PROFILE_NAME in .env).
    5. Extracts workout titles and generates MyWhooshInfo.com workout URLs.
    6. Authenticates and uploads to Garmin Connect using Garth.
    7. Enriches Garmin Connect activity titles and notes with workout details & Coggan metrics.
    8. Automatically backs up .fit files to a structured Git repository (year/month) and updates
       an append-only rides_summary.csv and rides_index.json catalog with auto git commit/push.
    9. Moves processed/uploaded .fit files to an 'Archive' directory to prevent duplicate uploads.
    10. Logs all events, successes, errors, and auth failures to sync.log.
"""

import os
import sys
import time
import shutil
import logging
from pathlib import Path
import warnings
import re
import csv
import json
import base64
import struct
import subprocess
from datetime import datetime, timezone
from getpass import getpass
from typing import List, Optional, Tuple, Any, Dict
from dataclasses import dataclass, asdict, field

# Suppress library deprecation notices in output
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Load environment variables from .env file
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=SCRIPT_DIR / ".env")

# Configure Logging
LOG_FILE_PATH = SCRIPT_DIR / "sync.log"
logger = logging.getLogger("MyWhoosh2Garmin")
logger.setLevel(logging.INFO)

# File and Console Handlers
if not logger.handlers:
    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

# Dependencies
try:
    import garth
    from garth.exc import GarthException, GarthHTTPError
    from fit_tool.fit_file import FitFile
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.profile_type import Manufacturer, GarminProduct, Sport, SubSport
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
    from fit_tool.profile.messages.user_profile_message import UserProfileMessage
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
    from fit_tool.profile.messages.course_message import CourseMessage
    from fit_tool.profile.messages.record_message import (
        RecordMessage,
        RecordTemperatureField,
    )
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.messages.lap_message import LapMessage
    from fit_tool.profile.messages.zones_target_message import ZonesTargetMessage
except ImportError as e:
    logger.critical(f"Missing required dependency: {e}. Please run 'pip install -r requirements.txt'.")
    sys.exit(1)

TOKENS_PATH = SCRIPT_DIR / ".garth"
MYWHOOSH_PREFIX_WINDOWS = "MyWhooshTechnologyService."

SUMMARY_CSV_HEADERS = [
    "Date",
    "ActivityID",
    "Sport",
    "Workout Name",
    "MyWhooshInfo URL",
    "Duration (s)",
    "Distance (km)",
    "Avg Power (W)",
    "NP (W)",
    "Max Power (W)",
    "Avg Cadence (RPM)",
    "Avg HR (BPM)",
    "TSS",
    "IF",
    "Work (kJ)",
    "Fit Filename",
]

GENERIC_ACTIVITY_NAMES = {
    "indoor cycling",
    "cycling",
    "virtual cycling",
    "indoor rowing",
    "rowing",
    "activity",
    "biking",
    "mywhoosh",
    "edge 1030 plus",
    "garmin edge",
    "garmin connect",
    "workout",
    "ride",
    "session",
    "lap",
    "record",
    "event",
    "course",
    "file_id",
    "file_creator",
    "device_info",
    "developer_data_id",
    "field_description",
    "user_profile",
    "zones_target",
    "mynewactivity",
    "new activity",
    "untitled",
}


@dataclass
class RideTelemetry:
    """Encapsulates all computed and extracted ride/activity telemetry."""
    date_str: str               # "YYYY-MM-DD HH:MM:SS"
    activity_id: str            # Garmin Connect Activity ID (or "")
    sport: str                  # e.g. "Cycling", "Rowing"
    workout_name: str           # e.g. "Spiked Aerobic #1"
    mywhooshinfo_url: str       # e.g. "https://mywhooshinfo.com/workouts/workout/spiked-aerobic-1"
    duration_sec: int           # Total duration in seconds
    distance_km: float          # Distance in kilometers
    avg_power: int              # Average Power in Watts
    np_power: int               # Normalized Power in Watts
    max_power: int              # Maximum Power in Watts
    avg_cadence: int            # Average Cadence in RPM / SPM
    avg_hr: int                 # Average Heart Rate in BPM
    tss: float                  # Training Stress Score
    intensity_factor: float     # Intensity Factor
    work_kj: int                # Total work in kJ
    fit_filename: str           # Source .fit filename
    year: str                   # "YYYY"
    month: str                  # "MM"
    athlete_id: str = "andrew"  # Matched athlete identifier


@dataclass
class AthleteProfile:
    """Configuration for an individual MyWhoosh athlete and their Garmin/archive settings."""
    user_id: str
    display_name: str
    mywhoosh_matches: List[str] = field(default_factory=list)
    ftp: int = 0
    garmin_enabled: bool = True
    garmin_email: str = ""
    garmin_password: str = ""
    archive_subdir: str = ""
    token_dir: Path = field(default_factory=lambda: TOKENS_PATH)

    def matches_identifier(self, candidate: Optional[str]) -> bool:
        if not candidate:
            return False
        cand_clean = str(candidate).strip().lower()
        if not cand_clean:
            return False
        targets = {
            self.user_id.strip().lower(),
            self.display_name.strip().lower(),
        }
        if self.garmin_email:
            targets.add(self.garmin_email.strip().lower())
        for m in self.mywhoosh_matches:
            if m and str(m).strip():
                targets.add(str(m).strip().lower())
        return cand_clean in targets


@dataclass
class DetectedMyWhooshAccount:
    """Represents a MyWhoosh account discovered from local SaveGames files."""
    email: str = ""
    player_name: str = ""
    user_uuid: str = ""
    source_file: str = ""

    def summary(self) -> str:
        parts = []
        if self.player_name:
            parts.append(f"name='{self.player_name}'")
        if self.email:
            parts.append(f"email='{self.email}'")
        if self.user_uuid:
            parts.append(f"uuid='{self.user_uuid}'")
        return ", ".join(parts) if parts else "unknown"


# Environment Configuration
def _get_env_clean(key: str, default: str = "") -> str:
    val = os.getenv(key, default) or ""
    return val.strip().strip("\"'")


def _get_bool_env(key: str, default: bool = True) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "y", "t", "on")


GARMIN_EMAIL = _get_env_clean("GARMIN_EMAIL") or _get_env_clean("GARMIN_USERNAME")
GARMIN_PASSWORD = _get_env_clean("GARMIN_PASSWORD")
MYWHOOSH_PROFILE_NAME = _get_env_clean("MYWHOOSH_PROFILE_NAME")
ENV_WORKOUTS_DIR = _get_env_clean("MYWHOOSH_WORKOUTS_DIR")
ENV_ARCHIVE_DIR = _get_env_clean("ARCHIVE_DIR")

raw_ftp = _get_env_clean("ATHLETE_FTP")
try:
    ATHLETE_FTP = int(raw_ftp) if raw_ftp else 0
except ValueError:
    logger.warning(f"Invalid ATHLETE_FTP value '{raw_ftp}' in .env. Defaulting to 0.")
    ATHLETE_FTP = 0

# Garmin Activity Metadata Enrichment
UPDATE_GARMIN_METADATA = _get_bool_env("UPDATE_GARMIN_METADATA", default=True)
APPEND_MYWHOOSHINFO_URL = _get_bool_env("APPEND_MYWHOOSHINFO_URL", default=True)

# GitHub Repository Backup & Indexing
ENABLE_GIT_BACKUP = _get_bool_env("ENABLE_GIT_BACKUP", default=True)
GIT_BACKUP_REPO_PATH = _get_env_clean("GIT_BACKUP_REPO_PATH")
GIT_BACKUP_SUBDIR = _get_env_clean("GIT_BACKUP_SUBDIR", default="auto")
GIT_AUTO_PUSH = _get_bool_env("GIT_AUTO_PUSH", default=True)

# Virtual Rowing Georeferencing (Lake Bled, Slovenia)
ROWING_GEOREFERENCE_ENABLED = _get_bool_env("ROWING_GEOREFERENCE_ENABLED", default=True)
try:
    LAKE_BLED_START_LAT = float(_get_env_clean("LAKE_BLED_START_LAT", "46.3606") or 46.3606)
except ValueError:
    LAKE_BLED_START_LAT = 46.3606

try:
    LAKE_BLED_START_LON = float(_get_env_clean("LAKE_BLED_START_LON", "14.0905") or 14.0905)
except ValueError:
    LAKE_BLED_START_LON = 14.0905

try:
    LAKE_BLED_CIRCUIT_METERS = float(_get_env_clean("LAKE_BLED_CIRCUIT_METERS", "9500.0") or 9500.0)
except ValueError:
    LAKE_BLED_CIRCUIT_METERS = 9500.0


def calculate_lake_bled_position(
    distance_m: float,
    circuit_length_m: float = LAKE_BLED_CIRCUIT_METERS,
    start_lat: float = LAKE_BLED_START_LAT,
    start_lon: float = LAKE_BLED_START_LON,
) -> Tuple[float, float]:
    """
    Computes georeferenced (lat, lon) coordinates on Lake Bled (Slovenia) along the 
    MyWhoosh 10km lozenge (stadium) circuit based on accumulated distance.
    Starts at the southern channel (46.3608° N, 14.0905° E) south-southeast of Bled Island,
    runs in a straight line North-East along the channel towards the eastern bay,
    sweeps around the eastern bay in open water, runs in a straight line Westbound
    along the northern shore under the castle cliffs, and sweeps around Bled Island
    back to the start.
    """
    import math

    # Normalized fraction of full lap (0.0 to 1.0)
    frac = (distance_m % circuit_length_m) / circuit_length_m

    # Key landmark coordinates defining the lozenge on Lake Bled (100% in water)
    south_end_lat = 46.3642
    south_end_lon = 14.1015

    east_center_lat = 46.3660
    east_center_lon = 14.1015
    r_lat = 0.0018
    r_lon = 0.0028

    north_start_lat = 46.3678
    north_start_lon = 14.1015

    north_end_lat = 46.3665
    north_end_lon = 14.0875

    west_center_lat = 46.3636
    west_center_lon = 14.0875
    w_r_lat = 0.0028
    w_r_lon = 0.0035

    # Seg 1 (0.00 to 0.38): Southern straightaway heading North-East (start -> south_end)
    if frac < 0.38:
        t = frac / 0.38
        lat = start_lat + t * (south_end_lat - start_lat)
        lon = start_lon + t * (south_end_lon - start_lon)
    # Seg 2 (0.38 to 0.50): Eastern turn in open water (sweeps 180° counter-clockwise to north_start)
    elif frac < 0.50:
        t = (frac - 0.38) / 0.12
        angle = -math.pi / 2.0 + t * math.pi
        lat = east_center_lat + r_lat * math.sin(angle)
        lon = east_center_lon + r_lon * math.cos(angle)
    # Seg 3 (0.50 to 0.88): Northern straightaway heading Westbound under castle (north_start -> north_end)
    elif frac < 0.88:
        t = (frac - 0.50) / 0.38
        lat = north_start_lat + t * (north_end_lat - north_start_lat)
        lon = north_start_lon + t * (north_end_lon - north_start_lon)
    # Seg 4 (0.88 to 1.00): Western turn looping 180° counter-clockwise around Bled Island back to start
    else:
        t = (frac - 0.88) / 0.12
        angle = math.pi / 2.0 + t * math.pi
        lat = west_center_lat + w_r_lat * math.sin(angle)
        lon = west_center_lon + w_r_lon * math.cos(angle)

    return round(lat, 7), round(lon, 7)





def extract_fit_sport(fit_file: FitFile) -> Tuple[str, str, int, str]:
    """
    Detects the sport from SessionMessage.
    Returns (sport_name, sport_dir, spoof_product_id, spoof_product_name)
    e.g. ("Cycling", "cycling", GarminProduct.EDGE_1030_PLUS.value, "Edge 1030 Plus")
    or   ("Rowing", "rowing", GarminProduct.FENIX7.value, "Fenix 7")
    """
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, SessionMessage):
            sport_val = getattr(msg, "sport", None)
            sub_sport_val = getattr(msg, "sub_sport", None)
            if sport_val == Sport.ROWING.value or sub_sport_val == SubSport.INDOOR_ROWING.value:
                return "Rowing", "rowing", GarminProduct.FENIX7.value, "Fenix 7"
            if sport_val == Sport.CYCLING.value or sub_sport_val in (
                SubSport.INDOOR_CYCLING.value,
                SubSport.VIRTUAL_ACTIVITY.value,
            ):
                return "Cycling", "cycling", GarminProduct.EDGE_1030_PLUS.value, "Edge 1030 Plus"

    return "Cycling", "cycling", GarminProduct.EDGE_1030_PLUS.value, "Edge 1030 Plus"



def parse_fit_timestamp(ts: Optional[int], fallback_time: Optional[float] = None) -> datetime:
    """Parses integer/float FIT timestamp into a datetime object."""
    if ts is not None and ts > 0:
        if ts > 100_000_000_000:
            return datetime.fromtimestamp(ts / 1000.0)
        elif ts > 100_000_000:
            return datetime.fromtimestamp(ts)
        else:
            return datetime.fromtimestamp(ts + 631065600)
    if fallback_time:
        return datetime.fromtimestamp(fallback_time)
    return datetime.now()


def clean_extracted_name(name: Any) -> Optional[str]:
    """Cleans up a candidate workout name, ignoring generic, internal, or empty titles."""
    if not name:
        return None
    val = str(name).strip()
    if not val:
        return None
    val_lower = val.lower()
    if val_lower in GENERIC_ACTIVITY_NAMES or val_lower.startswith("mynewactivity"):
        return None
    return val


_WORKOUT_CATALOG_CACHE: Optional[List[Dict[str, Any]]] = None


def find_mywhoosh_cache_dirs() -> List[Path]:
    """
    Discovers potential MyWhoosh persistent cache / workout directories
    across Windows Store, standard Windows installs, and macOS.
    """
    dirs: List[Path] = []
    # 1. Custom path override if configured
    if ENV_WORKOUTS_DIR:
        custom = Path(ENV_WORKOUTS_DIR).expanduser().resolve()
        for parent in (custom, custom.parent, custom.parent.parent):
            cache_p = parent / "PersistentDownloadDir" / "DefaultCache"
            if cache_p.is_dir() and cache_p not in dirs:
                dirs.append(cache_p)

    # 2. Windows Path Discovery
    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            base_local = Path(local_app_data)
            # Standard MyWhoosh installs
            for std in ("MyWhoosh", "MyWhooshHD"):
                p = base_local / std / "Saved" / "PersistentDownloadDir" / "DefaultCache"
                if p.is_dir() and p not in dirs:
                    dirs.append(p)
            # Windows Store Packages
            packages_path = base_local / "Packages"
            if packages_path.is_dir():
                try:
                    for pkg in packages_path.iterdir():
                        if pkg.is_dir() and pkg.name.startswith(MYWHOOSH_PREFIX_WINDOWS):
                            p = (
                                pkg
                                / "LocalCache"
                                / "Local"
                                / "MyWhoosh"
                                / "Saved"
                                / "PersistentDownloadDir"
                                / "DefaultCache"
                            )
                            if p.is_dir() and p not in dirs:
                                dirs.append(p)
                except Exception:
                    pass

    # 3. macOS Discovery
    elif os.name == "posix":
        mac_paths = [
            Path.home()
            / "Library"
            / "Containers"
            / "com.whoosh.whooshgame"
            / "Data"
            / "Library"
            / "Application Support"
            / "Epic"
            / "MyWhoosh"
            / "Saved"
            / "PersistentDownloadDir"
            / "DefaultCache",
            Path.home()
            / "Library"
            / "Application Support"
            / "MyWhoosh"
            / "Saved"
            / "PersistentDownloadDir"
            / "DefaultCache",
        ]
        for mp in mac_paths:
            if mp.is_dir() and mp not in dirs:
                dirs.append(mp)

    return dirs


def load_mywhoosh_workout_catalog() -> List[Dict[str, Any]]:
    """
    Loads structured workout definitions:
    1. Loads bundled workouts_catalog.json from SCRIPT_DIR if present (immediate offline/cross-platform fallback).
    2. Merges any additional definitions found in MyWhoosh's local cache directories.
    Each entry contains: 'id', 'name', 'durations' (list of step times in seconds), and 'file'.
    """
    global _WORKOUT_CATALOG_CACHE
    if _WORKOUT_CATALOG_CACHE is not None:
        return _WORKOUT_CATALOG_CACHE

    catalog: List[Dict[str, Any]] = []
    seen_ids = set()

    # 1. Bundled catalog (provides 730+ workouts out-of-the-box on any OS without path dependencies)
    bundled_file = SCRIPT_DIR / "workouts_catalog.json"
    if bundled_file.exists():
        try:
            with open(bundled_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "name" in item and "durations" in item:
                            catalog.append(item)
                            if item.get("id"):
                                seen_ids.add(item["id"])
            logger.info(f"Loaded {len(catalog)} workout definitions from bundled workouts_catalog.json.")
        except Exception as e:
            logger.warning(f"Error reading bundled workout catalog '{bundled_file.name}': {e}")

    # 2. Local cache directory discovery (to pick up newly downloaded workouts from MyWhoosh)
    cache_dirs = find_mywhoosh_cache_dirs()
    new_from_cache = 0
    for cache_dir in cache_dirs:
        for json_file in cache_dir.rglob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "Name" in data and "WorkoutStepsArray" in data:
                        w_id = data.get("ID")
                        if w_id and w_id in seen_ids:
                            continue
                        w_name = str(data["Name"]).strip()
                        durations = [
                            int(round(s.get("Time", 0)))
                            for s in data.get("WorkoutStepsArray", [])
                        ]
                        if durations and w_name:
                            catalog.append(
                                {
                                    "id": w_id,
                                    "name": w_name,
                                    "durations": durations,
                                    "file": json_file.name,
                                }
                            )
                            if w_id:
                                seen_ids.add(w_id)
                            new_from_cache += 1
            except Exception:
                pass

    if new_from_cache > 0:
        logger.info(f"Discovered {new_from_cache} additional workout definition(s) from local MyWhoosh cache.")

    logger.info(f"Total active workout definitions in catalog: {len(catalog)}.")
    _WORKOUT_CATALOG_CACHE = catalog
    return _WORKOUT_CATALOG_CACHE



def match_workout_from_laps(lap_durations: List[int]) -> Optional[Tuple[str, str]]:
    """
    Matches a sequence of FIT lap durations against the MyWhoosh workout library.
    Handles:
    1. Exact full workout match
    2. Fuzzy full workout match (+/- 2s per step)
    3. Workout completed followed by post-workout free-ride extension (extra laps at end)
    4. Workout with prolonged final cooldown step (last step extended by athlete)
    5. Workout preceded by free-ride warm-up (contiguous sub-sequence)
    6. Workout ended early (athlete completed >= 3 steps)
    Returns (workout_name, match_type) or None.
    """
    if not lap_durations or len(lap_durations) < 2:
        return None

    catalog = load_mywhoosh_workout_catalog()
    if not catalog:
        return None

    # Priority 1: Exact complete match across all laps
    for wkt in catalog:
        if wkt["durations"] == lap_durations:
            return wkt["name"], "exact"

    # Priority 2: Fuzzy complete match (+/- 2s per step) across all laps
    for wkt in catalog:
        if len(wkt["durations"]) == len(lap_durations):
            if all(abs(a - b) <= 2 for a, b in zip(lap_durations, wkt["durations"])):
                return wkt["name"], "fuzzy"

    # Priority 3: Completed workout followed by free-ride extension (laps start with full workout)
    # Picks the longest matching workout profile (e.g. 12 steps before free-ride)
    prefix_matches = []
    for wkt in catalog:
        w_durs = wkt["durations"]
        n = len(w_durs)
        if 2 <= n < len(lap_durations):
            if lap_durations[:n] == w_durs:
                prefix_matches.append((wkt["name"], "completed_plus_freeride", n))
            elif all(abs(a - b) <= 2 for a, b in zip(lap_durations[:n], w_durs)):
                prefix_matches.append((wkt["name"], "fuzzy_completed_plus_freeride", n))
    if prefix_matches:
        prefix_matches.sort(key=lambda x: x[2], reverse=True)
        return prefix_matches[0][0], prefix_matches[0][1]

    # Priority 4: Final cooldown step prolonged (steps 1..N-1 match, final step extended by athlete)
    cooldown_ext_matches = []
    for wkt in catalog:
        w_durs = wkt["durations"]
        n = len(w_durs)
        if n >= 3 and len(lap_durations) == n:
            if lap_durations[: n - 1] == w_durs[: n - 1] and lap_durations[n - 1] >= w_durs[n - 1]:
                cooldown_ext_matches.append((wkt["name"], "extended_cooldown", n))
    if cooldown_ext_matches:
        cooldown_ext_matches.sort(key=lambda x: x[2], reverse=True)
        return cooldown_ext_matches[0][0], cooldown_ext_matches[0][1]

    # Priority 5: Free-ride warm-up before workout (contiguous sub-sequence)
    subseq_matches = []
    for wkt in catalog:
        w_durs = wkt["durations"]
        n = len(w_durs)
        if n >= 3 and len(lap_durations) > n:
            for start in range(1, len(lap_durations) - n + 1):
                window = lap_durations[start : start + n]
                if window == w_durs:
                    subseq_matches.append((wkt["name"], f"subsequence_offset_{start}", n))
                    break
                elif all(abs(a - b) <= 2 for a, b in zip(window, w_durs)):
                    subseq_matches.append((wkt["name"], f"fuzzy_subsequence_offset_{start}", n))
                    break
    if subseq_matches:
        subseq_matches.sort(key=lambda x: x[2], reverse=True)
        return subseq_matches[0][0], subseq_matches[0][1]

    # Priority 6: Athlete cut workout short (at least 3 steps completed)
    if len(lap_durations) >= 3:
        for wkt in catalog:
            w_durs = wkt["durations"]
            if len(w_durs) > len(lap_durations):
                if w_durs[: len(lap_durations)] == lap_durations:
                    return wkt["name"], "early_exit"
                elif all(abs(a - b) <= 2 for a, b in zip(lap_durations, w_durs[: len(lap_durations)])):
                    return wkt["name"], "fuzzy_early_exit"

    return None


def extract_workout_name(
    fit_file: FitFile, fallback_stem: str, ride_date_str: str, sport_name: str = "Cycling"
) -> Tuple[str, bool]:
    """
    Extracts workout title from FIT records:
    1. WorkoutMessage.wkt_name / WorkoutMessage.workout_name
    2. General message field scan for wkt_name / workout_name / workout_title / program_name
    3. SessionMessage.sport_profile_name (ignoring SessionMessage.name which is library 'session')
    4. CourseMessage.name (if non-generic)
    5. Lap duration profile match against local MyWhoosh structured workout catalog
    6. Fallback from clean filename stem or formatted date string

    Returns:
        (workout_title: str, is_specific_workout: bool)
    """
    # 1. WorkoutMessage
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, WorkoutMessage):
            w_name = clean_extracted_name(getattr(msg, "wkt_name", None)) or clean_extracted_name(
                getattr(msg, "workout_name", None)
            )
            if w_name:
                return w_name, True

    # 2. General field scan across all message records
    for record in fit_file.records:
        msg = record.message
        for attr in ("wkt_name", "workout_name", "workout_title", "program_name"):
            if hasattr(msg, attr):
                val = clean_extracted_name(getattr(msg, attr, None))
                if val:
                    return val, True

    # 3. SessionMessage - inspect custom sport profile name (ignore internal fit_tool msg.name = 'session')
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, SessionMessage):
            s_name = clean_extracted_name(getattr(msg, "sport_profile_name", None))
            if s_name:
                return s_name, True

    # 4. CourseMessage
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, CourseMessage):
            c_name = clean_extracted_name(getattr(msg, "name", None))
            if c_name:
                return c_name, False

    # 5. Match workout from FIT Lap duration profile against local MyWhoosh workout library
    laps = [r.message for r in fit_file.records if isinstance(r.message, LapMessage)]
    lap_durations = [
        int(round(getattr(lap, "total_timer_time", 0) or 0))
        for lap in laps
        if getattr(lap, "total_timer_time", 0)
    ]
    matched = match_workout_from_laps(lap_durations)
    if matched:
        matched_name, match_type = matched
        logger.info(
            f"Identified MyWhoosh workout '{matched_name}' "
            f"({match_type} lap profile match across {len(lap_durations)} steps)."
        )
        return matched_name, True

    # 6. Fallback from filename stem or date
    if fallback_stem:
        cleaned_stem = clean_extracted_name(re.sub(r"[-_]+", " ", fallback_stem).strip())
        if cleaned_stem and not re.match(r"^\d{4}[-_ ]\d{2}[-_ ]\d{2}", cleaned_stem):
            return cleaned_stem.title(), True

    return f"MyWhoosh {sport_name} ({ride_date_str})", False


def generate_workout_slug(workout_name: str) -> str:
    """
    Generates a normalized URL slug for MyWhooshInfo from workout name:
    e.g. 'Speed Sprints' -> 'speed-sprints', 'Endurance with 30/30\'s' -> 'endurance-with-30-30-s'
    Returns empty string for generic or default activity names.
    """
    if not workout_name:
        return ""
    cleaned = clean_extracted_name(workout_name)
    if not cleaned or cleaned.lower().startswith("mywhoosh "):
        return ""
    slug = cleaned.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def get_mywhooshinfo_url(workout_name: str) -> str:
    """
    Constructs the MyWhooshInfo workout details URL. Returns empty string if not a specific workout.
    """
    slug = generate_workout_slug(workout_name)
    if not slug:
        return ""
    return f"https://mywhooshinfo.com/workouts/workout/{slug}"


def extract_fit_timestamp(fit_file: FitFile, fallback_mtime: Optional[float] = None) -> datetime:
    """
    Extracts start datetime from SessionMessage, FileIdMessage, or file modification time.
    """
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, SessionMessage):
            st = getattr(msg, "start_time", None) or getattr(msg, "timestamp", None)
            if st:
                return parse_fit_timestamp(st, fallback_mtime)
        elif isinstance(msg, FileIdMessage):
            tc = getattr(msg, "time_created", None)
            if tc:
                return parse_fit_timestamp(tc, fallback_mtime)

    if fallback_mtime:
        return datetime.fromtimestamp(fallback_mtime)
    return datetime.now()


def get_fitfile_location() -> Optional[Path]:
    """
    Automatically discovers the MyWhoosh workout directory.
    Uses LOCALAPPDATA environment variable on Windows with fallbacks.
    """
    # 1. Custom directory override from .env
    if ENV_WORKOUTS_DIR:
        custom_path = Path(ENV_WORKOUTS_DIR).expanduser().resolve()
        if custom_path.is_dir():
            logger.info(f"Using configured MyWhoosh workout path from .env: {custom_path}")
            return custom_path
        logger.warning(f"Configured MYWHOOSH_WORKOUTS_DIR does not exist: {custom_path}")

    # 2. Windows Path Discovery
    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            base_local = Path(local_app_data)

            # Standard MyWhoosh install locations
            standard_candidates = [
                base_local / "MyWhoosh" / "Saved" / "Workouts",
                base_local / "MyWhooshHD" / "Saved" / "Workouts",
                base_local / "MyWhoosh" / "Content" / "Data",
            ]

            for candidate in standard_candidates:
                if candidate.is_dir():
                    logger.info(f"Discovered MyWhoosh directory: {candidate}")
                    return candidate

            # Windows Store / Package install location
            packages_path = base_local / "Packages"
            if packages_path.is_dir():
                try:
                    for directory in packages_path.iterdir():
                        if directory.is_dir() and directory.name.startswith(MYWHOOSH_PREFIX_WINDOWS):
                            store_candidates = [
                                directory / "LocalCache" / "Local" / "MyWhoosh" / "Saved" / "Workouts",
                                directory / "LocalCache" / "Local" / "MyWhoosh" / "Content" / "Data",
                            ]
                            for s_cand in store_candidates:
                                if s_cand.is_dir():
                                    logger.info(f"Discovered MyWhoosh Store directory: {s_cand}")
                                    return s_cand
                except (PermissionError, FileNotFoundError) as e:
                    logger.warning(f"Error scanning Windows Packages directory: {e}")

        # Fallback check under User Profile
        user_profile = Path.home() / "AppData" / "Local" / "MyWhoosh" / "Saved" / "Workouts"
        if user_profile.is_dir():
            return user_profile

    # 3. POSIX Path Discovery (macOS / Linux)
    elif os.name == "posix":
        mac_path = (
            Path.home()
            / "Library"
            / "Containers"
            / "com.whoosh.whooshgame"
            / "Data"
            / "Library"
            / "Application Support"
            / "Epic"
            / "MyWhoosh"
            / "Content"
            / "Data"
        )
        if mac_path.is_dir():
            logger.info(f"Discovered MyWhoosh macOS path: {mac_path}")
            return mac_path

    logger.error("Could not automatically locate MyWhoosh workout directory. Set MYWHOOSH_WORKOUTS_DIR in .env.")
    return None


def get_archive_location(workout_dir: Path) -> Path:
    """
    Returns and creates the Archive directory path.
    """
    if ENV_ARCHIVE_DIR:
        archive_path = Path(ENV_ARCHIVE_DIR).expanduser().resolve()
    else:
        archive_path = workout_dir / "Archive"

    archive_path.mkdir(parents=True, exist_ok=True)
    return archive_path


USERS_CONFIG_PATH = SCRIPT_DIR / "users.json"


def load_users_config(config_path: Optional[Path] = None) -> Tuple[Dict[str, AthleteProfile], str]:
    """
    Loads multi-user athlete profiles from users.json (with .env fallback if absent).
    Returns (users_dict, unassigned_policy).
    unassigned_policy is 'hold' (default: hold in Archive/unassigned) or 'default:<user_id>'.
    """
    target_path = config_path or USERS_CONFIG_PATH
    users: Dict[str, AthleteProfile] = {}
    unassigned_policy = "hold"

    if target_path.exists():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                unassigned_policy = str(data.get("unassigned_policy", "hold") or "hold").strip().lower()
                raw_users = data.get("users", {})
                if isinstance(raw_users, dict):
                    for uid, ucfg in raw_users.items():
                        if not isinstance(ucfg, dict):
                            continue
                        clean_id = str(uid).strip().lower()
                        disp = str(ucfg.get("display_name") or uid).strip()
                        matches = [str(m).strip() for m in ucfg.get("mywhoosh_matches", []) if str(m).strip()]
                        try:
                            u_ftp = int(ucfg.get("ftp") or 0)
                        except (ValueError, TypeError):
                            u_ftp = 0
                        if u_ftp <= 0 and clean_id == "andrew" and ATHLETE_FTP > 0:
                            u_ftp = ATHLETE_FTP

                        g_enabled = bool(ucfg.get("garmin_enabled", True))
                        email_env = str(ucfg.get("garmin_email_env") or "").strip()
                        pass_env = str(ucfg.get("garmin_password_env") or "").strip()
                        g_email = str(ucfg.get("garmin_email") or "").strip()
                        g_pass = str(ucfg.get("garmin_password") or "").strip()
                        if not g_email and email_env:
                            g_email = _get_env_clean(email_env)
                        if not g_pass and pass_env:
                            g_pass = _get_env_clean(pass_env)

                        subdir = str(ucfg.get("archive_subdir") or clean_id).strip()
                        tok_dir = TOKENS_PATH / clean_id
                        users[clean_id] = AthleteProfile(
                            user_id=clean_id,
                            display_name=disp,
                            mywhoosh_matches=matches,
                            ftp=u_ftp,
                            garmin_enabled=g_enabled,
                            garmin_email=g_email,
                            garmin_password=g_pass,
                            archive_subdir=subdir,
                            token_dir=tok_dir,
                        )
        except Exception as e:
            logger.warning(f"Failed to load '{target_path.name}': {e}. Falling back to .env profile.")

    if not users:
        default_id = (MYWHOOSH_PROFILE_NAME or "andrew").strip().lower()
        default_disp = MYWHOOSH_PROFILE_NAME or "Andrew"
        matches = [default_disp, "Andrew", "Andrew Westley", "andy@andrewwestley.co.uk"]
        users[default_id] = AthleteProfile(
            user_id=default_id,
            display_name=default_disp,
            mywhoosh_matches=matches,
            ftp=ATHLETE_FTP,
            garmin_enabled=True,
            garmin_email=GARMIN_EMAIL,
            garmin_password=GARMIN_PASSWORD,
            archive_subdir=default_id,
            token_dir=TOKENS_PATH / default_id,
        )

    return users, unassigned_policy


def find_mywhoosh_savegames_dirs(workout_dir: Optional[Path] = None) -> List[Path]:
    """
    Locates MyWhoosh's 'Saved/SaveGames' directories across Windows Store, standard Windows, and macOS.
    """
    dirs: List[Path] = []
    if workout_dir:
        for cand in (
            workout_dir / "SaveGames",
            workout_dir.parent / "SaveGames",
            workout_dir.parent.parent / "Saved" / "SaveGames",
        ):
            if cand.is_dir() and cand not in dirs:
                dirs.append(cand)

    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            base_local = Path(local_app_data)
            for std in ("MyWhoosh", "MyWhooshHD"):
                p = base_local / std / "Saved" / "SaveGames"
                if p.is_dir() and p not in dirs:
                    dirs.append(p)
            packages_path = base_local / "Packages"
            if packages_path.is_dir():
                try:
                    for pkg in packages_path.iterdir():
                        if pkg.is_dir() and pkg.name.startswith(MYWHOOSH_PREFIX_WINDOWS):
                            p = pkg / "LocalCache" / "Local" / "MyWhoosh" / "Saved" / "SaveGames"
                            if p.is_dir() and p not in dirs:
                                dirs.append(p)
                except Exception:
                    pass
    elif os.name == "posix":
        for mp in (
            Path.home()
            / "Library"
            / "Containers"
            / "com.whoosh.whooshgame"
            / "Data"
            / "Library"
            / "Application Support"
            / "Epic"
            / "MyWhoosh"
            / "Saved"
            / "SaveGames",
            Path.home()
            / "Library"
            / "Application Support"
            / "MyWhoosh"
            / "Saved"
            / "SaveGames",
        ):
            if mp.is_dir() and mp not in dirs:
                dirs.append(mp)

    return dirs


def extract_gvas_str_properties(data: bytes, prop_name: str) -> List[str]:
    """
    Extracts Unreal Engine GVAS StrProperty values for a given property name from .sav binary data.
    """
    marker = prop_name.encode("ascii") + b"\x00\x0c\x00\x00\x00StrProperty\x00"
    results: List[str] = []
    idx = 0
    while True:
        pos = data.find(marker, idx)
        if pos == -1:
            break
        val_offset = pos + len(marker) + 9
        if val_offset + 4 <= len(data):
            try:
                (slen,) = struct.unpack_from("<i", data, val_offset)
                if 0 < slen <= 1024 and val_offset + 4 + slen <= len(data):
                    raw_bytes = data[val_offset + 4 : val_offset + 4 + slen]
                    val = raw_bytes.rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
                    if val:
                        results.append(val)
            except Exception:
                pass
        idx = pos + len(marker)
    return results


def _decode_mywhoosh_email(raw_val: str) -> str:
    """Decodes base64-encoded MyWhoosh playerEmail if encoded, else returns stripped string."""
    val = (raw_val or "").strip()
    if not val or "@" in val:
        return val
    try:
        dec = base64.b64decode(val).decode("utf-8", errors="ignore").strip()
        if "@" in dec:
            return dec
    except Exception:
        pass
    return val


def list_all_mywhoosh_savegame_accounts(workout_dir: Optional[Path] = None) -> List[DetectedMyWhooshAccount]:
    """
    Scans MyWhoosh SaveGames (GameData.sav, OfflineData.sav, AchData-*.sav) and returns all
    discovered accounts on this machine, with the currently active account listed first.
    """
    accounts: List[DetectedMyWhooshAccount] = []
    seen_keys = set()

    for sg_dir in find_mywhoosh_savegames_dirs(workout_dir):
        # Collect UserId UUIDs from AchData-*.sav sorted by most recently modified
        ach_uuids: List[str] = []
        try:
            ach_files = sorted(sg_dir.glob("AchData-*.sav"), key=lambda p: p.stat().st_mtime, reverse=True)
            for af in ach_files:
                uids = extract_gvas_str_properties(af.read_bytes(), "UserId")
                for u in uids:
                    if u not in ach_uuids:
                        ach_uuids.append(u)
        except Exception:
            pass

        for sav_name in ("GameData.sav", "OfflineData.sav"):
            sav_path = sg_dir / sav_name
            if not sav_path.is_file():
                continue
            try:
                raw = sav_path.read_bytes()
            except Exception:
                continue

            cur_devs = extract_gvas_str_properties(raw, "CurrentDeviceId")
            active_email = ""
            if cur_devs and "::" in cur_devs[0]:
                active_email = cur_devs[0].split("::", 1)[0].strip()

            emails = [_decode_mywhoosh_email(e) for e in extract_gvas_str_properties(raw, "playerEmail")]
            names = extract_gvas_str_properties(raw, "PlayerName")

            email_to_name: Dict[str, str] = {}
            for idx, em in enumerate(emails):
                if em:
                    email_to_name[em.lower()] = names[idx] if idx < len(names) else ""

            if active_email:
                act_name = email_to_name.get(active_email.lower(), names[0] if len(names) == 1 else "")
                act_uuid = ach_uuids[0] if (len(ach_uuids) == 1 and len(emails) <= 1) else ""
                key = (active_email.lower(), act_name.lower())
                if key not in seen_keys:
                    seen_keys.add(key)
                    accounts.append(
                        DetectedMyWhooshAccount(
                            email=active_email,
                            player_name=act_name,
                            user_uuid=act_uuid,
                            source_file=str(sav_path),
                        )
                    )

            for idx, em in enumerate(emails):
                pname = names[idx] if idx < len(names) else ""
                key = (em.lower(), pname.lower())
                if key not in seen_keys:
                    seen_keys.add(key)
                    accounts.append(
                        DetectedMyWhooshAccount(
                            email=em,
                            player_name=pname,
                            user_uuid=ach_uuids[0] if len(ach_uuids) == 1 and len(emails) == 1 else "",
                            source_file=str(sav_path),
                        )
                    )

    return accounts


def detect_active_mywhoosh_account(
    workout_dir: Optional[Path] = None, fit_mtime: Optional[float] = None
) -> Optional[DetectedMyWhooshAccount]:
    """
    Determines the active MyWhoosh account from SaveGames at the time of the workout.
    Also checks AchData-<UUID>.sav modification times closest to fit_mtime when multiple UUIDs exist.
    """
    accounts = list_all_mywhoosh_savegame_accounts(workout_dir)
    best_uuid = ""
    best_uuid_diff = float("inf")

    for sg_dir in find_mywhoosh_savegames_dirs(workout_dir):
        try:
            for af in sg_dir.glob("AchData-*.sav"):
                uids = extract_gvas_str_properties(af.read_bytes(), "UserId")
                if not uids:
                    continue
                if fit_mtime is not None:
                    diff = abs(af.stat().st_mtime - fit_mtime)
                    if diff < best_uuid_diff:
                        best_uuid_diff = diff
                        best_uuid = uids[0]
                elif not best_uuid:
                    best_uuid = uids[0]
        except Exception:
            pass

    if accounts:
        primary = accounts[0]
        if best_uuid and not primary.user_uuid and len(accounts) == 1:
            primary.user_uuid = best_uuid
        return primary

    if best_uuid:
        return DetectedMyWhooshAccount(user_uuid=best_uuid, source_file="AchData")

    return None


def resolve_workout_athlete(
    fit_file: FitFile,
    fit_path: Path,
    users: Dict[str, AthleteProfile],
    unassigned_policy: str = "hold",
    workout_dir: Optional[Path] = None,
) -> Tuple[Optional[AthleteProfile], str]:
    """
    Determines which configured AthleteProfile owns a given .fit workout file.
    Priority order:
      1. Parent subfolder or filename prefix (e.g. 'andrew__ride.fit' or 'Workouts/andrew/ride.fit')
      2. Embedded FIT UserProfileMessage (friendly_name / name)
      3. Active MyWhoosh SaveGames account (CurrentDeviceId email, PlayerName, or AchData UserId UUID)
      4. Embedded FIT functional_threshold_power if uniquely matching one user (when SaveGames has no conflict)
      5. Fallback policy (if SaveGames is absent in a standalone test environment or unassigned_policy='default:<id>')
    Returns (matched_profile_or_None, match_description).
    """
    # 1. Explicit subfolder or filename prefix (<user_id>__ or <user_id>_)
    parent_name = fit_path.parent.name.strip().lower()
    if parent_name in users:
        return users[parent_name], f"subfolder '{parent_name}'"

    stem_lower = fit_path.stem.lower()
    for uid, prof in users.items():
        if stem_lower.startswith(f"{uid}__") or stem_lower.startswith(f"{uid}-"):
            return prof, f"filename prefix '{uid}'"

    # 2. Embedded FIT UserProfileMessage
    fit_profile_name = extract_fit_profile_name(fit_file)
    if fit_profile_name:
        for prof in users.values():
            if prof.matches_identifier(fit_profile_name):
                return prof, f"FIT UserProfileMessage '{fit_profile_name}'"
        return None, f"unrecognized FIT UserProfileMessage '{fit_profile_name}'"

    # 3. MyWhoosh SaveGames active account inspection
    try:
        fit_mtime = fit_path.stat().st_mtime
    except Exception:
        fit_mtime = None

    active_acct = detect_active_mywhoosh_account(workout_dir=workout_dir, fit_mtime=fit_mtime)
    if active_acct:
        if active_acct.email or active_acct.player_name:
            for prof in users.values():
                if prof.matches_identifier(active_acct.email) or prof.matches_identifier(active_acct.player_name):
                    return prof, f"MyWhoosh SaveGames ({active_acct.summary()})"
        elif active_acct.user_uuid:
            for prof in users.values():
                if prof.matches_identifier(active_acct.user_uuid):
                    return prof, f"MyWhoosh SaveGames ({active_acct.summary()})"
        # Active SaveGames account found, but does not match any configured user!
        return None, f"unconfigured MyWhoosh SaveGames account ({active_acct.summary()})"

    # 4. Embedded FIT FTP match (if unique)
    file_ftp = extract_fit_ftp(fit_file)
    if file_ftp and file_ftp > 0:
        ftp_matches = [p for p in users.values() if p.ftp == file_ftp]
        if len(ftp_matches) == 1:
            return ftp_matches[0], f"embedded FIT FTP ({file_ftp}W)"

    # 5. Policy fallback when no SaveGames or FIT profile exists (e.g. synthetic test files)
    if unassigned_policy.startswith("default:"):
        def_id = unassigned_policy.split(":", 1)[1].strip().lower()
        if def_id in users:
            return users[def_id], f"configured default user '{def_id}'"

    if len(users) == 1 and active_acct is None:
        only_user = next(iter(users.values()))
        return only_user, f"single configured user fallback ('{only_user.user_id}')"

    return None, "no matching athlete profile in FIT file or MyWhoosh SaveGames"


def _ensure_user_token_dir(athlete: Optional[AthleteProfile]) -> Path:
    """
    Returns the Garth token directory for the given athlete.
    If athlete is 'andrew' (or default) and .garth/andrew/ does not exist yet,
    copies existing root .garth/*.json token files so existing sessions continue seamlessly.
    """
    if athlete is None:
        return TOKENS_PATH

    user_token_dir = athlete.token_dir
    if not user_token_dir.exists() and TOKENS_PATH.is_dir():
        root_oauth1 = TOKENS_PATH / "oauth1_token.json"
        root_oauth2 = TOKENS_PATH / "oauth2_token.json"
        if athlete.user_id == "andrew" and (root_oauth1.exists() or root_oauth2.exists()):
            try:
                user_token_dir.mkdir(parents=True, exist_ok=True)
                for tf in (root_oauth1, root_oauth2):
                    if tf.exists():
                        shutil.copy2(str(tf), str(user_token_dir / tf.name))
                logger.info(f"Migrated existing Garmin OAuth tokens into '{user_token_dir}'.")
            except Exception as e:
                logger.debug(f"Could not copy root tokens to '{user_token_dir}': {e}")

    return user_token_dir


def authenticate_to_garmin(athlete: Optional[AthleteProfile] = None) -> bool:
    """
    Authenticates to Garmin Connect for a specific AthleteProfile (or legacy default)
    using isolated per-user token storage (.garth/<user_id>/) or .env credentials.
    """
    token_dir = _ensure_user_token_dir(athlete)
    email = athlete.garmin_email if athlete else GARMIN_EMAIL
    password = athlete.garmin_password if athlete else GARMIN_PASSWORD
    label = f"{athlete.display_name} ({athlete.user_id})" if athlete else "default user"

    # Try resuming existing token session
    if token_dir.exists() and any(token_dir.glob("*.json")):
        try:
            garth.resume(str(token_dir))
            username = getattr(garth.client, "username", None) or label
            logger.info(f"Successfully resumed Garmin session for {label} (account: {username})")
            return True
        except (GarthException, Exception) as e:
            logger.warning(f"Saved Garmin session token for {label} expired or invalid ({e}). Re-authenticating...")

    # Authenticate with credentials from profile / .env
    if email and password:
        logger.info(f"Authenticating to Garmin Connect for {label} using email: {email}")
        try:
            garth.login(email, password)
            token_dir.mkdir(parents=True, exist_ok=True)
            garth.save(str(token_dir))
            logger.info(f"Successfully authenticated and saved Garmin session tokens for {label} in '{token_dir}'.")
            return True
        except GarthHTTPError as e:
            logger.error(f"Authentication failed for {label}: Invalid Garmin credentials ({e}).")
            return False
        except Exception as e:
            logger.error(f"Unexpected authentication error for {label}: {e}")
            return False

    # Interactive Fallback if run manually from console
    if sys.stdin.isatty():
        logger.info(f"No stored token or credentials for {label}. Prompting for interactive login...")
        try:
            username = input(f"Garmin Username/Email for {label}: ").strip()
            pwd = getpass(f"Garmin Password for {label}: ")
            garth.login(username, pwd)
            token_dir.mkdir(parents=True, exist_ok=True)
            garth.save(str(token_dir))
            logger.info(f"Successfully authenticated {label} via interactive prompt.")
            return True
        except Exception as e:
            logger.error(f"Interactive authentication failed for {label}: {e}")
            return False

    logger.error(f"Garmin authentication failed for {label}: No valid session token in '{token_dir}' or credentials found.")
    return False


def calculate_avg(values: list) -> int:
    """Calculates integer average of values, returning 0 if empty."""
    return round(sum(values) / len(values)) if values else 0


def calculate_normalized_power(power_values: list) -> int:
    """
    Calculates Normalized Power (NP®) using Dr. Andrew Coggan's algorithm:
    1. 30-second rolling moving average.
    2. 4th-power of each 30s rolling value.
    3. Average of 4th-power values.
    4. 4th root of the average.
    """
    if not power_values:
        return 0
    if len(power_values) < 30:
        return calculate_avg(power_values)

    rolling_30s = []
    window_sum = sum(power_values[:30])
    rolling_30s.append(window_sum / 30.0)
    for i in range(30, len(power_values)):
        window_sum += power_values[i] - power_values[i - 30]
        rolling_30s.append(window_sum / 30.0)

    if not rolling_30s:
        return 0

    avg_pow4 = sum(p ** 4 for p in rolling_30s) / len(rolling_30s)
    return round(avg_pow4 ** 0.25)


def calculate_intensity_factor(np_val: int, ftp: int) -> float:
    """Calculates Intensity Factor (IF = NP / FTP)."""
    if not ftp or ftp <= 0:
        return 0.0
    return round(np_val / ftp, 3)


def calculate_tss(duration_sec: int, np_val: int, if_val: float, ftp: int) -> float:
    """Calculates Training Stress Score (TSS = (sec * NP * IF) / (FTP * 3600) * 100)."""
    if not ftp or ftp <= 0 or not duration_sec:
        return 0.0
    return round((duration_sec * np_val * if_val) / (ftp * 3600.0) * 100.0, 1)


def calculate_total_work_joules(power_values: list, duration_sec: Optional[int] = None) -> int:
    """Calculates total work in Joules (1 kJ = 1,000 J)."""
    if not power_values:
        return 0
    dur = duration_sec if duration_sec and duration_sec > 0 else len(power_values)
    avg_p = sum(power_values) / len(power_values)
    return round(avg_p * dur)


def extract_fit_profile_name(fit_file: FitFile) -> Optional[str]:
    """Extracts athlete/profile friendly name from FIT records if present."""
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, UserProfileMessage):
            name = getattr(msg, "friendly_name", None) or getattr(msg, "name", None)
            if name:
                return str(name).strip()
    return None


def extract_fit_ftp(fit_file: FitFile) -> Optional[int]:
    """Extracts configured FTP/threshold power from FIT records if present."""
    for record in fit_file.records:
        msg = record.message
        if hasattr(msg, "functional_threshold_power") and getattr(msg, "functional_threshold_power", None):
            return int(msg.functional_threshold_power)
        if hasattr(msg, "threshold_power") and getattr(msg, "threshold_power", None):
            return int(msg.threshold_power)
    return None


def clone_message(old_msg: object, msg_class: type) -> object:
    """
    Clones all valid fields from an existing FIT message into a fresh message instance.
    This allows dynamically adding/modifying fields that were not defined in the input file.
    """
    new_msg = msg_class()
    if hasattr(old_msg, "fields"):
        for old_field in old_msg.fields:
            if old_field.is_valid():
                new_field = new_msg.get_field(old_field.field_id)
                if new_field:
                    try:
                        new_field.set_values(old_field.get_values())
                    except Exception:
                        pass
    return new_msg


def process_and_spoof_fit_file(
    input_fit_path: Path,
    output_fit_path: Path,
    expected_profile: Optional[str] = None,
    athlete: Optional[AthleteProfile] = None,
) -> Tuple[bool, Optional[RideTelemetry]]:
    """
    Parses, validates profile, calculates advanced cycling metrics (NP, IF, TSS, Work, FTP),
    cleans up metrics, applies Garmin Edge 1030 Plus / Fenix 7 device spoofing, and extracts full ride telemetry.

    Returns:
        (bool, Optional[RideTelemetry]): (True, telemetry) if successful, (False, None) otherwise.
    """
    try:
        fit_file = FitFile.from_file(str(input_fit_path))
    except Exception as e:
        logger.error(f"Failed to read FIT file {input_fit_path.name}: {e}")
        return False, None

    # Multi-User Profile Check (legacy expected_profile filter when called directly)
    if expected_profile and athlete is None:
        file_profile = extract_fit_profile_name(fit_file)
        if file_profile:
            if file_profile.lower() != expected_profile.lower():
                logger.warning(
                    f"Skipping '{input_fit_path.name}': Athlete profile '{file_profile}' "
                    f"does not match configured target profile '{expected_profile}'."
                )
                return False, None
            logger.info(f"Validated athlete profile match: '{file_profile}'")
        else:
            logger.info(f"No athlete profile name embedded in {input_fit_path.name}. Proceeding with processing.")

    # Determine FTP: Extract from FIT file if present, else athlete profile FTP, else ATHLETE_FTP from .env
    file_ftp = extract_fit_ftp(fit_file)
    configured_ftp = athlete.ftp if (athlete and athlete.ftp > 0) else ATHLETE_FTP
    effective_ftp = file_ftp if file_ftp and file_ftp > 0 else configured_ftp
    if effective_ftp > 0:
        ftp_source = (
            "from FIT file"
            if file_ftp
            else (f"from user profile '{athlete.user_id}'" if athlete else "from .env")
        )
        logger.info(f"Using FTP: {effective_ftp}W ({ftp_source})")
    else:
        logger.warning("No FTP configured or found in FIT file. Set ftp in users.json or ATHLETE_FTP in .env for TSS/IF calculations.")

    # Detect Sport & Dynamic Device Spoofing (e.g. Edge 1030 Plus for Cycling, Fenix 7 for Rowing)
    sport_name, sport_dir, spoof_product_id, spoof_product_name = extract_fit_sport(fit_file)
    logger.info(f"Detected Activity Sport: '{sport_name}' -> Spoofing as Garmin {spoof_product_name}")

    # Extract Ride Start Date & Workout Title
    ride_dt = extract_fit_timestamp(fit_file, fallback_mtime=input_fit_path.stat().st_mtime)
    ride_date_str = ride_dt.strftime("%Y-%m-%d %H:%M:%S")
    workout_title, is_workout = extract_workout_name(
        fit_file, input_fit_path.stem, ride_date_str, sport_name=sport_name
    )
    mywhooshinfo_url = get_mywhooshinfo_url(workout_title) if is_workout else ""

    logger.info(f"Extracted Activity Title: '{workout_title}' | Slug URL: {mywhooshinfo_url or 'N/A'}")

    # Check if raw FIT file already has valid real-world coordinates (outside Null Island)
    raw_has_valid_gps = False
    for r in fit_file.records:
        if isinstance(r.message, RecordMessage):
            lat = getattr(r.message, "position_lat", None)
            lon = getattr(r.message, "position_long", None)
            if lat is not None and lon is not None:
                if abs(lat) > 2.0 or abs(lon) > 2.0:
                    raw_has_valid_gps = True
                    break

    if sport_name == "Rowing" and ROWING_GEOREFERENCE_ENABLED and not raw_has_valid_gps:
        logger.info(
            f"Georeferencing rowing activity to Lake Bled, Slovenia (circuit start: {LAKE_BLED_START_LAT}°N, {LAKE_BLED_START_LON}°E)."
        )

    builder = FitFileBuilder()

    # Lap-level and Session-level metric accumulators
    lap_cadence_values: List[int] = []
    lap_power_values: List[int] = []
    lap_heart_rate_values: List[int] = []

    session_cadence_values: List[int] = []
    session_power_values: List[int] = []
    session_heart_rate_values: List[int] = []

    lap_start_pos: Optional[Tuple[float, float]] = None
    lap_end_pos: Optional[Tuple[float, float]] = None
    session_start_pos: Optional[Tuple[float, float]] = None
    session_end_pos: Optional[Tuple[float, float]] = None

    total_distance_m = 0.0
    session_dur_val = 0

    for record in fit_file.records:
        message = record.message

        # 1. Device Spoofing: File ID message
        if isinstance(message, FileIdMessage):
            try:
                message.manufacturer = Manufacturer.GARMIN.value
            except Exception:
                pass
            try:
                message.product = spoof_product_id
            except Exception:
                pass
            if hasattr(message, "garmin_product"):
                try:
                    message.garmin_product = spoof_product_id
                except Exception:
                    pass

        # 2. Device Spoofing: Device Info message
        elif isinstance(message, DeviceInfoMessage):
            try:
                message.manufacturer = Manufacturer.GARMIN.value
            except Exception:
                pass
            try:
                message.product = spoof_product_id
            except Exception:
                pass
            if hasattr(message, "garmin_product"):
                try:
                    message.garmin_product = spoof_product_id
                except Exception:
                    pass
            try:
                message.product_name = spoof_product_name
            except Exception:
                pass

        # 3. Zones Target Message: Update FTP if present
        elif isinstance(message, ZonesTargetMessage):
            message = clone_message(message, ZonesTargetMessage)
            if effective_ftp > 0:
                try:
                    message.functional_threshold_power = effective_ftp
                except Exception:
                    pass

        # 4. Record Message: Remove temperature & accumulate metrics
        elif isinstance(message, RecordMessage):
            try:
                message.remove_field(RecordTemperatureField.ID)
            except Exception:
                pass

            p_val = getattr(message, "power", None)
            c_val = getattr(message, "cadence", None)
            hr_val = getattr(message, "heart_rate", None)
            d_val = getattr(message, "distance", None)

            if p_val is not None:
                lap_power_values.append(p_val)
                session_power_values.append(p_val)
            if c_val is not None:
                lap_cadence_values.append(c_val)
                session_cadence_values.append(c_val)
            if hr_val is not None:
                lap_heart_rate_values.append(hr_val)
                session_heart_rate_values.append(hr_val)
            if d_val is not None and float(d_val) > total_distance_m:
                total_distance_m = float(d_val)

            # Georeference Rowing activities if coordinates are missing or at Null Island
            if sport_name == "Rowing" and ROWING_GEOREFERENCE_ENABLED and not raw_has_valid_gps:
                cur_dist = float(d_val) if d_val is not None else total_distance_m
                g_lat, g_lon = calculate_lake_bled_position(cur_dist)
                try:
                    message.position_lat = g_lat
                    message.position_long = g_lon
                except Exception as e:
                    logger.debug(f"Could not set GPS coordinates on RecordMessage: {e}")

            # Track GPS positions for Lap and Session headers
            rec_lat = getattr(message, "position_lat", None)
            rec_lon = getattr(message, "position_long", None)
            if rec_lat is not None and rec_lon is not None:
                if lap_start_pos is None:
                    lap_start_pos = (rec_lat, rec_lon)
                lap_end_pos = (rec_lat, rec_lon)
                if session_start_pos is None:
                    session_start_pos = (rec_lat, rec_lon)
                session_end_pos = (rec_lat, rec_lon)

        # 5. Lap Message: Populate missing averages & power metrics
        elif isinstance(message, LapMessage):
            message = clone_message(message, LapMessage)
            if lap_start_pos and lap_end_pos:
                try:
                    if not getattr(message, "start_position_lat", None):
                        message.start_position_lat = lap_start_pos[0]
                    if not getattr(message, "start_position_long", None):
                        message.start_position_long = lap_start_pos[1]
                    if not getattr(message, "end_position_lat", None):
                        message.end_position_lat = lap_end_pos[0]
                    if not getattr(message, "end_position_long", None):
                        message.end_position_long = lap_end_pos[1]
                except Exception:
                    pass

            if lap_power_values:
                try:
                    if not getattr(message, "avg_power", None):
                        message.avg_power = calculate_avg(lap_power_values)
                except Exception:
                    pass
                try:
                    if not getattr(message, "max_power", None):
                        message.max_power = max(lap_power_values)
                except Exception:
                    pass
                try:
                    if not getattr(message, "normalized_power", None):
                        message.normalized_power = calculate_normalized_power(lap_power_values)
                except Exception:
                    pass
                try:
                    if not getattr(message, "total_work", None):
                        lap_dur = (
                            getattr(message, "total_timer_time", None)
                            or getattr(message, "total_elapsed_time", None)
                            or len(lap_power_values)
                        )
                        message.total_work = calculate_total_work_joules(lap_power_values, lap_dur)
                except Exception:
                    pass
            if lap_cadence_values:
                try:
                    if not getattr(message, "avg_cadence", None):
                        message.avg_cadence = calculate_avg(lap_cadence_values)
                except Exception:
                    pass
                try:
                    if not getattr(message, "max_cadence", None):
                        message.max_cadence = max(lap_cadence_values)
                except Exception:
                    pass
            if lap_heart_rate_values:
                try:
                    if not getattr(message, "avg_heart_rate", None):
                        message.avg_heart_rate = calculate_avg(lap_heart_rate_values)
                except Exception:
                    pass
                try:
                    if not getattr(message, "max_heart_rate", None):
                        message.max_heart_rate = max(lap_heart_rate_values)
                except Exception:
                    pass

            # Reset lap accumulators for the next lap
            lap_cadence_values = []
            lap_power_values = []
            lap_heart_rate_values = []
            lap_start_pos = None
            lap_end_pos = None

        # 6. Session Message: Populate missing averages, NP, IF, TSS, Work, and FTP
        elif isinstance(message, SessionMessage):
            message = clone_message(message, SessionMessage)
            if session_start_pos and session_end_pos:
                try:
                    if not getattr(message, "start_position_lat", None):
                        message.start_position_lat = session_start_pos[0]
                    if not getattr(message, "start_position_long", None):
                        message.start_position_long = session_start_pos[1]
                    if not getattr(message, "end_position_lat", None):
                        message.end_position_lat = session_end_pos[0]
                    if not getattr(message, "end_position_long", None):
                        message.end_position_long = session_end_pos[1]
                except Exception:
                    pass

            s_dist = getattr(message, "total_distance", None)
            if s_dist is not None:
                total_distance_m = float(s_dist)

            session_dur_val = (
                getattr(message, "total_timer_time", None)
                or getattr(message, "total_elapsed_time", None)
                or len(session_power_values)
            )

            if session_power_values:
                try:
                    if not getattr(message, "avg_power", None):
                        message.avg_power = calculate_avg(session_power_values)
                except Exception:
                    pass
                try:
                    if not getattr(message, "max_power", None):
                        message.max_power = max(session_power_values)
                except Exception:
                    pass

                # Normalized Power (NP)
                np_val = calculate_normalized_power(session_power_values)
                try:
                    message.normalized_power = np_val
                except Exception:
                    pass

                # Total Work (Joules)
                try:
                    message.total_work = calculate_total_work_joules(session_power_values, session_dur_val)
                except Exception:
                    pass

                # Advanced Metrics: IF, TSS, Threshold Power (FTP)
                if effective_ftp > 0:
                    try:
                        message.threshold_power = effective_ftp
                    except Exception:
                        pass

                    if_val = calculate_intensity_factor(
                        np_val or calculate_avg(session_power_values), effective_ftp
                    )
                    try:
                        message.intensity_factor = if_val
                    except Exception:
                        pass

                    tss_val = calculate_tss(
                        session_dur_val,
                        np_val or calculate_avg(session_power_values),
                        if_val,
                        effective_ftp,
                    )
                    try:
                        message.training_stress_score = tss_val
                    except Exception:
                        pass

                    total_work_val = getattr(message, "total_work", None) or 0
                    logger.info(
                        f"Computed Session Metrics: Avg Power={getattr(message, 'avg_power', 'N/A')}W, "
                        f"Max Power={getattr(message, 'max_power', 'N/A')}W, NP={np_val}W, IF={if_val}, "
                        f"TSS={tss_val}, Work={round(total_work_val / 1000)}kJ, FTP={effective_ftp}W"
                    )

            if session_cadence_values:
                try:
                    if not getattr(message, "avg_cadence", None):
                        message.avg_cadence = calculate_avg(session_cadence_values)
                except Exception:
                    pass
                try:
                    if not getattr(message, "max_cadence", None):
                        message.max_cadence = max(session_cadence_values)
                except Exception:
                    pass

            if session_heart_rate_values:
                try:
                    if not getattr(message, "avg_heart_rate", None):
                        message.avg_heart_rate = calculate_avg(session_heart_rate_values)
                except Exception:
                    pass
                try:
                    if not getattr(message, "max_heart_rate", None):
                        message.max_heart_rate = max(session_heart_rate_values)
                except Exception:
                    pass

        builder.add(message)

    try:
        output_fit_path.parent.mkdir(parents=True, exist_ok=True)
        builder.build().to_file(str(output_fit_path))
        logger.info(f"Successfully processed and spoofed '{input_fit_path.name}' -> Garmin {spoof_product_name} ({sport_name}).")
    except Exception as e:
        logger.error(f"Failed to write processed FIT file {output_fit_path.name}: {e}")
        return False, None

    # Compile Final Ride Telemetry
    np_final = calculate_normalized_power(session_power_values)
    avg_p_final = calculate_avg(session_power_values)
    max_p_final = max(session_power_values) if session_power_values else 0
    avg_c_final = calculate_avg(session_cadence_values)
    avg_hr_final = calculate_avg(session_heart_rate_values)
    dur_final = int(session_dur_val) if session_dur_val else len(session_power_values)
    dist_km_final = round(total_distance_m / 1000.0, 2)
    work_j_final = calculate_total_work_joules(session_power_values, dur_final)
    work_kj_final = round(work_j_final / 1000.0)

    if_final = calculate_intensity_factor(np_final or avg_p_final, effective_ftp)
    tss_final = calculate_tss(dur_final, np_final or avg_p_final, if_final, effective_ftp)

    telemetry = RideTelemetry(
        date_str=ride_date_str,
        activity_id="",
        sport=sport_name,
        workout_name=workout_title,
        mywhooshinfo_url=mywhooshinfo_url,
        duration_sec=dur_final,
        distance_km=dist_km_final,
        avg_power=avg_p_final,
        np_power=np_final,
        max_power=max_p_final,
        avg_cadence=avg_c_final,
        avg_hr=avg_hr_final,
        tss=tss_final,
        intensity_factor=if_final,
        work_kj=work_kj_final,
        fit_filename=input_fit_path.name,
        year=ride_dt.strftime("%Y"),
        month=ride_dt.strftime("%m"),
        athlete_id=athlete.user_id if athlete else "andrew",
    )

    return True, telemetry


def archive_file(source_file: Path, archive_dir: Path, target_name: Optional[str] = None) -> Optional[Path]:
    """
    Moves a processed file into the archive folder, handling filename collisions.
    Optionally renames the file using target_name if provided.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    file_name = target_name or source_file.name
    destination = archive_dir / file_name

    if destination.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = archive_dir / f"{destination.stem}_{timestamp}{destination.suffix}"

    try:
        shutil.move(str(source_file), str(destination))
        logger.info(f"Archived '{source_file.name}' to '{destination}'.")
        return destination
    except Exception as e:
        logger.error(f"Failed to archive '{source_file.name}': {e}")
        return None


def extract_activity_id_from_upload_response(upload_res: Any) -> Optional[int]:
    """
    Extracts the Garmin activityId from garth.client.upload response dict.
    """
    if not upload_res or not isinstance(upload_res, dict):
        return None

    # 1. detailedImportResult
    detail = upload_res.get("detailedImportResult")
    if isinstance(detail, dict):
        if detail.get("internalId"):
            try:
                return int(detail["internalId"])
            except (ValueError, TypeError):
                pass
        if detail.get("activityId"):
            try:
                return int(detail["activityId"])
            except (ValueError, TypeError):
                pass
        successes = detail.get("successes")
        if isinstance(successes, list) and successes:
            for item in successes:
                if isinstance(item, dict):
                    if item.get("internalId"):
                        return int(item["internalId"])
                    if item.get("activityId"):
                        return int(item["activityId"])

    # 2. Top-level keys
    for key in ("activityId", "internalId", "id", "activity_id"):
        if key in upload_res and upload_res[key]:
            try:
                return int(upload_res[key])
            except (ValueError, TypeError):
                pass

    return None


def extract_upload_id(upload_res: Any) -> Optional[int]:
    """Extracts uploadId from upload response."""
    if not upload_res or not isinstance(upload_res, dict):
        return None
    detail = upload_res.get("detailedImportResult")
    if isinstance(detail, dict) and detail.get("uploadId"):
        try:
            return int(detail["uploadId"])
        except (ValueError, TypeError):
            pass
    if "uploadId" in upload_res and upload_res["uploadId"]:
        try:
            return int(upload_res["uploadId"])
        except (ValueError, TypeError):
            pass
    return None


def poll_garmin_upload_status(upload_id: int, max_retries: int = 5) -> Optional[int]:
    """
    Polls Garmin's upload status endpoint for the exact uploadId until the activityId is returned.
    This guarantees 100% precision by querying the upload transaction directly.
    """
    for attempt in range(1, max_retries + 1):
        time.sleep(2)
        try:
            status_res = garth.client.connectapi(f"/upload-service/upload/status/{upload_id}")
            act_id = extract_activity_id_from_upload_response(status_res)
            if act_id:
                logger.info(f"Retrieved activity ID {act_id} from upload {upload_id} status poll.")
                return act_id
        except Exception as e:
            logger.debug(f"Error checking upload status for {upload_id}: {e}")
    logger.warning(f"Could not retrieve activity ID from upload {upload_id} status polling.")
    return None


def update_garmin_activity_metadata(
    activity_id: int,
    workout_name: str,
    slug: str,
    np_val: int,
    if_val: float,
    tss_val: float,
    work_kj: int,
    append_url: bool = True,
) -> bool:
    """
    Updates the Garmin activity title and description with workout details & Coggan metrics.
    """
    lines = []
    if slug:
        lines.append(f"Workout: {workout_name}")
        if append_url:
            lines.append(f"Details: https://mywhooshinfo.com/workouts/workout/{slug}")
    else:
        lines.append(f"Activity: {workout_name}")

    lines.append(f"Metrics: NP: {np_val}W | IF: {if_val} | TSS: {tss_val} | Work: {work_kj}kJ")
    description = "\n".join(lines)

    endpoint = f"/activity-service/activity/{activity_id}"
    payload = {
        "activityId": activity_id,
        "activityName": workout_name,
        "description": description,
    }
    try:
        garth.client.connectapi(endpoint, method="PUT", json=payload)
        logger.info(
            f"Successfully enriched Garmin activity {activity_id}: "
            f"Title='{workout_name}', Description=\n{description}"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to update metadata for Garmin activity {activity_id}: {e}")
        return False


def is_exact_timestamp_match(act_str: Optional[str], expected_dt: datetime) -> bool:
    """
    Strictly validates that the candidate activity start time matches the FIT workout start time
    within a maximum 120-second window in either local time or UTC.
    """
    if not act_str:
        return False
    clean_ts = act_str.replace("T", " ").split(".")[0]
    try:
        act_dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False

    # 1. Compare directly against local datetime (within 120 seconds)
    if abs((act_dt - expected_dt).total_seconds()) <= 120:
        return True

    # 2. Compare against UTC datetime (within 120 seconds)
    try:
        local_tz = datetime.now().astimezone().tzinfo
        expected_aware = expected_dt.replace(tzinfo=local_tz)
        expected_utc = expected_aware.astimezone(timezone.utc).replace(tzinfo=None)
        if abs((act_dt - expected_utc).total_seconds()) <= 120:
            return True
    except Exception:
        pass

    return False


def find_activity_id_by_exact_timestamp(expected_dt: datetime, max_retries: int = 4) -> Optional[int]:
    """
    Searches recent activities, matching ONLY if the start time is within 120 seconds of the workout.
    Retries with backoff to give Garmin cloud ingestion time to complete.
    """
    for attempt in range(1, max_retries + 1):
        time.sleep(2 * attempt)
        try:
            activities = garth.client.connectapi(
                "/activitylist-service/activities/search/activities",
                params={"limit": 5},
            )
            if isinstance(activities, list) and len(activities) > 0:
                for act in activities:
                    if isinstance(act, dict) and "activityId" in act:
                        act_id = int(act["activityId"])
                        start_local = act.get("startTimeLocal")
                        start_gmt = act.get("startTimeGMT")
                        if is_exact_timestamp_match(start_local, expected_dt) or is_exact_timestamp_match(start_gmt, expected_dt):
                            logger.info(f"Retrieved verified Garmin activity ID via exact 120s timestamp match: {act_id}")
                            return act_id
        except Exception as e:
            logger.debug(f"Error querying activities search (attempt {attempt}): {e}")

    logger.warning(
        f"Could not find Garmin activity strictly matching workout start time ({expected_dt.strftime('%Y-%m-%d %H:%M:%S')}) "
        "within 120 seconds. Skipping metadata update to prevent touching unrelated activities."
    )
    return None


def upload_fit_file_to_garmin(file_path: Path, expected_dt: Optional[datetime] = None) -> Tuple[bool, Optional[int]]:
    """
    Uploads a .fit file to Garmin Connect using Garth.
    Returns (success: bool, activity_id: Optional[int]).
    """
    try:
        with open(file_path, "rb") as f:
            uploaded = garth.client.upload(f)
            logger.info(f"Successfully uploaded {file_path.name} to Garmin Connect! (Response: {uploaded})")
            
            # 1. Check if activity ID was returned synchronously in the upload response
            act_id = extract_activity_id_from_upload_response(uploaded)
            
            # 2. Try polling the upload status directly using uploadId
            if not act_id:
                up_id = extract_upload_id(uploaded)
                if up_id:
                    act_id = poll_garmin_upload_status(up_id)

            # 3. Fallback: Search recent activities with a strict 120-second timestamp match
            if not act_id and expected_dt:
                act_id = find_activity_id_by_exact_timestamp(expected_dt)
            
            return True, act_id
    except GarthHTTPError as e:
        # HTTP 409 or duplicate activity error
        if "409" in str(e) or "duplicate" in str(e).lower():
            logger.warning(f"Activity {file_path.name} already exists on Garmin Connect (duplicate detected).")
            return True, None
        err_detail = ""
        if hasattr(e, "error") and hasattr(e.error, "response") and e.error.response is not None:
            err_detail = f" | Response: {e.error.response.status_code} - {e.error.response.text}"
        logger.error(f"Garmin HTTP upload error for {file_path.name}: {e}{err_detail}")
        return False, None
    except Exception as e:
        logger.error(f"Garmin upload failed for {file_path.name}: [{type(e).__name__}] {e}")
        return False, None


def update_rides_catalog(backup_dir: Path, telemetry: RideTelemetry) -> None:
    """
    Maintains append-only / updated rides_summary.csv and rides_index.json in the backup repository.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    csv_path = backup_dir / "rides_summary.csv"
    json_path = backup_dir / "rides_index.json"

    row_dict = {
        "Date": telemetry.date_str,
        "ActivityID": str(telemetry.activity_id) if telemetry.activity_id else "",
        "Sport": telemetry.sport,
        "Workout Name": telemetry.workout_name,
        "MyWhooshInfo URL": telemetry.mywhooshinfo_url,
        "Duration (s)": telemetry.duration_sec,
        "Distance (km)": telemetry.distance_km,
        "Avg Power (W)": telemetry.avg_power,
        "NP (W)": telemetry.np_power,
        "Max Power (W)": telemetry.max_power,
        "Avg Cadence (RPM)": telemetry.avg_cadence,
        "Avg HR (BPM)": telemetry.avg_hr,
        "TSS": telemetry.tss,
        "IF": telemetry.intensity_factor,
        "Work (kJ)": telemetry.work_kj,
        "Fit Filename": telemetry.fit_filename,
    }

    # 1. Update CSV Catalog
    csv_rows = []
    csv_updated = False
    if csv_path.exists():
        try:
            with open(csv_path, mode="r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    # Ignore empty entries or stray git merge conflict markers
                    if not r or any(str(val or "").startswith(("<<<<<<<", "=======", ">>>>>>>")) for val in r.values()):
                        continue
                    if (
                        (telemetry.activity_id and r.get("ActivityID") == str(telemetry.activity_id))
                        or (telemetry.fit_filename and r.get("Fit Filename") == telemetry.fit_filename)
                        or (r.get("Date") == telemetry.date_str and r.get("Sport") == telemetry.sport)
                    ):
                        csv_rows.append(row_dict)
                        csv_updated = True
                    else:
                        csv_rows.append(r)
        except Exception as e:
            logger.warning(f"Error reading existing CSV catalog '{csv_path.name}': {e}")

    if not csv_updated:
        csv_rows.append(row_dict)

    try:
        with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_CSV_HEADERS)
            writer.writeheader()
            writer.writerows(csv_rows)
        logger.info(f"Updated ride summary catalog: '{csv_path}'.")
    except Exception as e:
        logger.error(f"Failed to write ride summary catalog '{csv_path}': {e}")

    # 2. Update JSON Index
    json_entries = []
    json_updated = False
    if json_path.exists():
        try:
            with open(json_path, mode="r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    json_entries = data
        except Exception as e:
            logger.warning(f"Error reading existing JSON index '{json_path.name}': {e}")

    for idx, entry in enumerate(json_entries):
        if isinstance(entry, dict) and (
            (telemetry.activity_id and entry.get("ActivityID") == str(telemetry.activity_id))
            or (telemetry.fit_filename and entry.get("Fit Filename") == telemetry.fit_filename)
            or (entry.get("Date") == telemetry.date_str and entry.get("Sport") == telemetry.sport)
        ):
            json_entries[idx] = row_dict
            json_updated = True
            break

    if not json_updated:
        json_entries.append(row_dict)

    try:
        with open(json_path, mode="w", encoding="utf-8") as f:
            json.dump(json_entries, f, indent=2)
        logger.info(f"Updated ride JSON index: '{json_path}'.")
    except Exception as e:
        logger.error(f"Failed to write ride JSON index '{json_path}': {e}")


def git_backup_repository(
    repo_path: Path,
    workout_name: str,
    ride_date_str: str,
    sport_name: str = "activity",
    auto_push: bool = True,
    athlete: Optional[AthleteProfile] = None,
) -> bool:
    """
    Performs non-blocking git add, commit, and push within the target backup repository.
    """
    if not repo_path.is_dir():
        logger.warning(f"Git backup skipped: Directory '{repo_path}' does not exist.")
        return False

    if not (repo_path / ".git").exists():
        logger.warning(f"Git backup skipped: '{repo_path}' is not a git repository (missing .git).")
        return False

    athlete_prefix = f"[{athlete.user_id}] " if athlete else ""
    commit_msg = f"Auto-backup {athlete_prefix}{sport_name.lower()}: {workout_name} ({ride_date_str})"

    # 1. git pull (to sync any remote deletions or edits from GitHub)
    try:
        pull_res = subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if pull_res.returncode != 0:
            logger.debug(f"git pull notice in {repo_path}: {pull_res.stderr.strip()}")
    except Exception as e:
        logger.debug(f"git pull error in {repo_path}: {e}")

    # 2. git add .
    try:
        add_res = subprocess.run(
            ["git", "add", "."],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if add_res.returncode != 0:
            logger.warning(f"git add failed in {repo_path}: {add_res.stderr.strip()}")
            return False
    except Exception as e:
        logger.warning(f"Failed to execute git add in {repo_path}: {e}")
        return False

    # 2. Check git status
    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if not status_res.stdout.strip():
            logger.info("No changes to commit in git backup repository.")
            return True
    except Exception as e:
        logger.warning(f"Failed to check git status in {repo_path}: {e}")

    # 3. git commit
    author_name = (athlete.display_name if athlete else MYWHOOSH_PROFILE_NAME) or "MyWhoosh Sync"
    author_email = (athlete.garmin_email if (athlete and athlete.garmin_email) else GARMIN_EMAIL) or "sync@mywhoosh2garmin.local"
    git_env = os.environ.copy()
    if "GIT_AUTHOR_NAME" not in git_env or not git_env.get("GIT_AUTHOR_NAME"):
        git_env["GIT_AUTHOR_NAME"] = author_name
    if "GIT_AUTHOR_EMAIL" not in git_env or not git_env.get("GIT_AUTHOR_EMAIL"):
        git_env["GIT_AUTHOR_EMAIL"] = author_email
    if "GIT_COMMITTER_NAME" not in git_env or not git_env.get("GIT_COMMITTER_NAME"):
        git_env["GIT_COMMITTER_NAME"] = author_name
    if "GIT_COMMITTER_EMAIL" not in git_env or not git_env.get("GIT_COMMITTER_EMAIL"):
        git_env["GIT_COMMITTER_EMAIL"] = author_email

    try:
        commit_res = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            env=git_env,
        )
        if commit_res.returncode != 0:
            logger.warning(f"git commit failed in {repo_path}: {commit_res.stderr.strip()}")
            return False
        logger.info(f"Committed git backup: '{commit_msg}'")
    except Exception as e:
        logger.warning(f"Failed to execute git commit in {repo_path}: {e}")
        return False

    # 4. git push with automatic retry on transient network/remote errors
    if auto_push:
        for attempt in range(1, 4):
            try:
                push_res = subprocess.run(
                    ["git", "push"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if push_res.returncode == 0:
                    logger.info("Successfully pushed ride backup to remote git repository.")
                    return True
                logger.warning(
                    f"git push attempt {attempt}/3 failed in {repo_path} (offline or remote rejected): {push_res.stderr.strip()}"
                )
                if attempt < 3:
                    time.sleep(2 * attempt)
            except Exception as e:
                logger.warning(f"Failed to execute git push (attempt {attempt}/3) in {repo_path}: {e}")
                if attempt < 3:
                    time.sleep(2 * attempt)
        return False

    return True


def sync_pending_git_backup(repo_path: Path, auto_push: bool = True) -> bool:
    """
    Checks if the backup repository has unpushed commits (e.g. from an earlier transient
    network outage or remote server error) and automatically pushes them to the remote repository.
    """
    if not auto_push or not repo_path.is_dir() or not (repo_path / ".git").exists():
        return False

    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain", "-b"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if status_res.returncode == 0 and "[ahead " in status_res.stdout:
            logger.info(f"Detected unpushed backup commits in '{repo_path}'. Catching up remote repository...")
            for attempt in range(1, 4):
                push_res = subprocess.run(
                    ["git", "push"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if push_res.returncode == 0:
                    logger.info("Successfully pushed pending backup commits to remote git repository.")
                    return True
                logger.warning(
                    f"git push catch-up attempt {attempt}/3 failed in {repo_path}: {push_res.stderr.strip()}"
                )
                if attempt < 3:
                    time.sleep(2 * attempt)
    except Exception as e:
        logger.debug(f"Could not check or push pending backup commits in {repo_path}: {e}")

    return False


def generate_backup_fit_filename(
    date_str: str,
    workout_name: str,
    sport_name: str,
    fallback_filename: str = "",
) -> str:
    """
    Generates a unique, standardized, collision-free filename for backed-up FIT files.
    Format: YYYYMMDD_HHMMSS_<slug>.fit
    Example: 20261019_074156_ftp_test_mywhoosh.fit
    """
    # 1. Format timestamp prefix (YYYYMMDD_HHMMSS)
    ts_prefix = ""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        ts_prefix = dt.strftime("%Y%m%d_%H%M%S")
    except Exception:
        digits = re.sub(r"[^\d]", "", date_str or "")
        if len(digits) >= 14:
            ts_prefix = f"{digits[:8]}_{digits[8:14]}"
        elif digits:
            ts_prefix = digits
        else:
            ts_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 2. Derive descriptive slug
    slug = ""
    if workout_name:
        cleaned = clean_extracted_name(workout_name)
        if cleaned and not cleaned.lower().startswith("mywhoosh "):
            # Strip date patterns if present in workout title
            cleaned = re.sub(r"\b\d{4}[-_]\d{2}[-_]\d{2}(?:[-_ ]\d{2}[-_:]\d{2}[-_:]\d{2})?\b", "", cleaned)
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", cleaned.lower()).strip("_")

    if not slug and sport_name:
        s_val = re.sub(r"[^a-zA-Z0-9]+", "_", str(sport_name).strip().lower()).strip("_")
        if s_val:
            slug = s_val

    if not slug and fallback_filename:
        stem = Path(fallback_filename).stem
        cleaned_stem = clean_extracted_name(stem)
        if cleaned_stem:
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", cleaned_stem.lower()).strip("_")

    if not slug:
        slug = "activity"

    return f"{ts_prefix}_{slug}.fit"


def migrate_legacy_backup_repo(repo_path: Path, default_user_subdir: str = "andrew") -> bool:
    """
    Migrates legacy root-level folders (cycling/, rowing/, raw/, rides_summary.csv, rides_index.json)
    into a dedicated top-level user directory (e.g., MyWhooshFitnessData/andrew/).
    Safe and idempotent: if root-level folders do not exist, does nothing.
    """
    if not repo_path.is_dir() or not default_user_subdir:
        return False

    user_root = repo_path / default_user_subdir
    legacy_dirs = ["cycling", "rowing", "raw"]
    legacy_files = ["rides_summary.csv", "rides_index.json"]

    has_legacy = any((repo_path / d).is_dir() for d in legacy_dirs) or any(
        (repo_path / f).is_file() for f in legacy_files
    )
    if not has_legacy:
        return False

    logger.info(f"Migrating root-level fitness archive data in '{repo_path}' into '{user_root}'...")
    user_root.mkdir(parents=True, exist_ok=True)

    # 1. Move directory trees (cycling, rowing, raw)
    for d_name in legacy_dirs:
        src_dir = repo_path / d_name
        if not src_dir.is_dir():
            continue
        dst_dir = user_root / d_name
        if not dst_dir.exists():
            shutil.move(str(src_dir), str(dst_dir))
        else:
            for item in src_dir.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(src_dir)
                    target_f = dst_dir / rel
                    target_f.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item), str(target_f))
            shutil.rmtree(str(src_dir), ignore_errors=True)

    # 2. Move index files (rides_summary.csv, rides_index.json)
    for f_name in legacy_files:
        src_file = repo_path / f_name
        if not src_file.is_file():
            continue
        dst_file = user_root / f_name
        if not dst_file.exists():
            shutil.move(str(src_file), str(dst_file))
        else:
            # Remove legacy root file if user_root already has one
            src_file.unlink(missing_ok=True)

    logger.info(f"Completed migration of legacy archive data to '{user_root}'.")
    return True


def backup_and_index_ride(
    source_fit_path: Path,
    telemetry: RideTelemetry,
    repo_path_str: str,
    subdir_str: str,
    auto_push: bool = True,
    original_fit_path: Optional[Path] = None,
    user_subdir: Optional[str] = None,
    athlete: Optional[AthleteProfile] = None,
) -> bool:
    """
    Copies FIT file to structured year/month folder in the backup repo with collision-free naming,
    optionally namespaces under a top-level user directory (e.g. 'andrew/', 'user2/'),
    preserves the raw original MyWhoosh extract in 'raw/', updates catalog, and commits.
    """
    if not repo_path_str:
        logger.warning("Git ride backup skipped: GIT_BACKUP_REPO_PATH is not configured.")
        return False

    repo_path = Path(repo_path_str).expanduser().resolve()
    if not repo_path.exists():
        try:
            repo_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create GIT_BACKUP_REPO_PATH '{repo_path}': {e}")
            return False

    # Base directory for this athlete (e.g. repo_path / 'andrew', or repo_path if user_subdir is None)
    user_base_dir = (repo_path / user_subdir) if user_subdir else repo_path
    user_base_dir.mkdir(parents=True, exist_ok=True)

    # Generate collision-free filename e.g. 20261019_074156_ftp_test_mywhoosh.fit
    backup_filename = generate_backup_fit_filename(
        date_str=telemetry.date_str,
        workout_name=telemetry.workout_name,
        sport_name=telemetry.sport,
        fallback_filename=source_fit_path.name,
    )
    # Ensure telemetry reflects the unique backup filename in catalog & index
    telemetry.fit_filename = backup_filename

    # Target folder:
    # If subdir_str == "auto": use telemetry.sport.lower() (e.g. 'cycling', 'rowing')
    # If subdir_str is custom: use that
    # If subdir_str is empty/blank: use user_base_dir directly
    target_dir = user_base_dir
    if subdir_str and subdir_str.lower() == "auto":
        target_dir = target_dir / telemetry.sport.lower()
    elif subdir_str:
        target_dir = target_dir / subdir_str

    target_dir = target_dir / telemetry.year / telemetry.month

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_fit = target_dir / backup_filename
        shutil.copy2(str(source_fit_path), str(dest_fit))
        logger.info(f"Backed up ride FIT file to '{dest_fit}'.")
    except Exception as e:
        logger.warning(f"Failed to copy FIT file to backup directory '{target_dir}': {e}")
        return False

    # Also back up the untouched original MyWhoosh extract to {user_base_dir}/raw/{sport}/{year}/{month}/
    if original_fit_path and original_fit_path.exists():
        try:
            raw_target_dir = user_base_dir / "raw"
            if subdir_str and subdir_str.lower() == "auto":
                raw_target_dir = raw_target_dir / telemetry.sport.lower()
            elif subdir_str:
                raw_target_dir = raw_target_dir / subdir_str
            raw_target_dir = raw_target_dir / telemetry.year / telemetry.month
            raw_target_dir.mkdir(parents=True, exist_ok=True)
            dest_raw = raw_target_dir / backup_filename
            shutil.copy2(str(original_fit_path), str(dest_raw))
            logger.info(f"Backed up raw untouched MyWhoosh extract to '{dest_raw}'.")
        except Exception as e:
            logger.warning(f"Failed to copy raw original FIT file to backup directory: {e}")

    # Update rides_summary.csv and rides_index.json inside user_base_dir
    try:
        update_rides_catalog(user_base_dir, telemetry)
    except Exception as e:
        logger.warning(f"Failed to update ride catalog in '{user_base_dir}': {e}")

    # Automated git commit & push at the root of the git repository
    try:
        git_backup_repository(
            repo_path=repo_path,
            workout_name=telemetry.workout_name,
            ride_date_str=telemetry.date_str,
            sport_name=telemetry.sport,
            auto_push=auto_push,
            athlete=athlete,
        )
    except Exception as e:
        logger.warning(f"Git auto-backup encountered an error: {e}")

    return True



def get_pending_fit_files(workout_dir: Path, archive_dir: Path) -> List[Path]:
    """
    Discovers all candidate .fit files in the workout directory (and optional user subfolders),
    ignoring the archive directory.
    """
    candidate_files = []
    for f in workout_dir.glob("*.fit"):
        if f.is_file() and archive_dir not in f.parents and f.parent == workout_dir:
            candidate_files.append(f)

    # Sort oldest to newest by modification time
    candidate_files.sort(key=lambda x: x.stat().st_mtime)
    return candidate_files


def auto_update_script_repo() -> None:
    """
    Attempts a quick 'git pull --ff-only' in SCRIPT_DIR so that improvements,
    catalog updates, and bug fixes pushed to GitHub are automatically applied
    on client machines (such as macOS background runners) without manual intervention.
    """
    if not (SCRIPT_DIR / ".git").exists():
        return
    try:
        res = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode == 0:
            out = res.stdout.strip()
            if out and "Already up to date" not in out:
                logger.info(f"Self-update: pulled latest changes from remote repository ({out}).")
    except Exception as e:
        logger.debug(f"Self-update skipped: {e}")


def main():
    """Main execution entry point."""
    logger.info("=== Starting MyWhoosh2Garmin Sync ===")

    # 0. Check for remote script updates
    auto_update_script_repo()

    # 1. Load Multi-User Profiles
    users, unassigned_policy = load_users_config()
    logger.info(
        f"Loaded {len(users)} configured athlete profile(s): "
        f"{', '.join(f'{u.display_name} ({u.user_id})' for u in users.values())}"
    )

    # Ensure legacy root archive repo is migrated into 'andrew/' and catch up any unpushed backup commits
    if ENABLE_GIT_BACKUP and GIT_BACKUP_REPO_PATH:
        repo_p = Path(GIT_BACKUP_REPO_PATH).expanduser().resolve()
        if repo_p.is_dir():
            migrate_legacy_backup_repo(repo_p, default_user_subdir="andrew")
            if GIT_AUTO_PUSH:
                sync_pending_git_backup(repo_p, auto_push=True)

    # 2. Discover Workout Path
    workout_dir = get_fitfile_location()
    if not workout_dir:
        logger.error("Aborting sync: Workout directory not found.")
        sys.exit(1)

    archive_dir = get_archive_location(workout_dir)
    pending_files = get_pending_fit_files(workout_dir, archive_dir)

    if not pending_files:
        logger.info("No pending .fit files found to process.")
        return

    logger.info(f"Found {len(pending_files)} pending workout file(s).")

    # 3. Temporary processing directory
    temp_dir = SCRIPT_DIR / "temp_processing"
    temp_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    unassigned_count = 0
    active_garmin_user_id: Optional[str] = None

    try:
        for fit_path in pending_files:
            logger.info(f"Processing '{fit_path.name}'...")
            temp_output = temp_dir / fit_path.name

            try:
                raw_fit = FitFile.from_file(str(fit_path))
            except Exception as e:
                logger.error(f"Failed to read FIT file {fit_path.name}: {e}")
                continue

            # Resolve which athlete performed this workout
            athlete, match_reason = resolve_workout_athlete(
                fit_file=raw_fit,
                fit_path=fit_path,
                users=users,
                unassigned_policy=unassigned_policy,
                workout_dir=workout_dir,
            )

            if athlete is None:
                unassigned_dir = archive_dir / "unassigned"
                logger.warning(
                    f"Unassigned workout '{fit_path.name}': {match_reason}. "
                    f"Holding file in '{unassigned_dir}' (not uploading to Garmin). "
                    f"Add this user to users.json and run 'python myWhoosh2Garmin.py --reprocess-unassigned'."
                )
                archive_file(fit_path, unassigned_dir)
                unassigned_count += 1
                continue

            logger.info(
                f"Matched workout '{fit_path.name}' -> Athlete '{athlete.display_name}' "
                f"({athlete.user_id}) via {match_reason}."
            )

            # Process, calculate metrics using athlete's FTP, and spoof device
            processed, telemetry = process_and_spoof_fit_file(
                input_fit_path=fit_path,
                output_fit_path=temp_output,
                athlete=athlete,
            )

            if not processed or telemetry is None:
                if temp_output.exists():
                    temp_output.unlink(missing_ok=True)
                continue

            # Upload to Garmin Connect (if enabled for this athlete)
            upload_success = False
            activity_id: Optional[int] = None

            if athlete.garmin_enabled:
                if active_garmin_user_id != athlete.user_id:
                    if not authenticate_to_garmin(athlete):
                        logger.error(
                            f"Skipping '{fit_path.name}': Garmin authentication failed for athlete "
                            f"'{athlete.display_name}' ({athlete.user_id})."
                        )
                        if temp_output.exists():
                            temp_output.unlink(missing_ok=True)
                        continue
                    active_garmin_user_id = athlete.user_id

                ride_dt_obj = datetime.strptime(telemetry.date_str, "%Y-%m-%d %H:%M:%S")
                upload_success, activity_id = upload_fit_file_to_garmin(temp_output, expected_dt=ride_dt_obj)
            else:
                logger.info(
                    f"Garmin sync is disabled for athlete '{athlete.display_name}' ({athlete.user_id}). "
                    "Skipping Garmin Connect upload and proceeding to archive backup."
                )
                upload_success = True

            if upload_success:
                if activity_id:
                    telemetry.activity_id = str(activity_id)

                # Enrich Garmin Activity metadata (Title, description with MyWhooshInfo and metrics)
                if athlete.garmin_enabled and UPDATE_GARMIN_METADATA and activity_id:
                    update_garmin_activity_metadata(
                        activity_id=activity_id,
                        workout_name=telemetry.workout_name,
                        slug=generate_workout_slug(telemetry.workout_name),
                        np_val=telemetry.np_power,
                        if_val=telemetry.intensity_factor,
                        tss_val=telemetry.tss,
                        work_kj=telemetry.work_kj,
                        append_url=APPEND_MYWHOOSHINFO_URL,
                    )

                # Automated GitHub Ride Backup & Telemetry Indexing (under athlete's top-level folder)
                if ENABLE_GIT_BACKUP:
                    if GIT_BACKUP_REPO_PATH:
                        backup_file_target = temp_output if temp_output.exists() else fit_path
                        backup_and_index_ride(
                            source_fit_path=backup_file_target,
                            telemetry=telemetry,
                            repo_path_str=GIT_BACKUP_REPO_PATH,
                            subdir_str=GIT_BACKUP_SUBDIR,
                            auto_push=GIT_AUTO_PUSH,
                            original_fit_path=fit_path,
                            user_subdir=athlete.archive_subdir or athlete.user_id,
                            athlete=athlete,
                        )
                    else:
                        logger.warning("GitHub backup is enabled (ENABLE_GIT_BACKUP=true), but GIT_BACKUP_REPO_PATH is empty in .env. Skipping Git backup.")
                else:
                    logger.info("GitHub ride backup is disabled (ENABLE_GIT_BACKUP=false).")

                # Clean up temp file
                if temp_output.exists():
                    temp_output.unlink(missing_ok=True)

                # Archive original workout file under Archive/<user_id>/
                user_archive_dir = archive_dir / athlete.user_id
                archive_file(fit_path, user_archive_dir, target_name=telemetry.fit_filename)
                success_count += 1
            else:
                logger.warning(f"File '{fit_path.name}' was not archived because Garmin upload failed.")
                if temp_output.exists():
                    temp_output.unlink(missing_ok=True)

    finally:
        # Clean up temp directory
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    logger.info(
        f"=== Sync Completed: {success_count} file(s) synced and archived, "
        f"{unassigned_count} unassigned file(s) held. ==="
    )


def print_user_detection_report() -> None:
    """
    CLI diagnostic (--detect-user / --list-users):
    Displays configured users from users.json and all MyWhoosh accounts discovered in SaveGames.
    """
    users, policy = load_users_config()
    workout_dir = get_fitfile_location()
    accounts = list_all_mywhoosh_savegame_accounts(workout_dir)
    active = detect_active_mywhoosh_account(workout_dir)

    print("\n=== Configured Users (users.json) ===")
    print(f"Unassigned Workout Policy: {policy}")
    for uid, prof in users.items():
        tok_status = "Saved" if (prof.token_dir.exists() and any(prof.token_dir.glob("*.json"))) else "Not Saved"
        print(
            f"  - [{uid}] {prof.display_name} | FTP={prof.ftp}W | "
            f"Garmin={'Enabled' if prof.garmin_enabled else 'Disabled'} (Token: {tok_status}) | "
            f"Archive='{prof.archive_subdir}/' | Matches={prof.mywhoosh_matches}"
        )

    print("\n=== Discovered MyWhoosh Accounts on This Machine (SaveGames) ===")
    if not accounts:
        print("  No MyWhoosh SaveGames accounts found.")
    else:
        for idx, acct in enumerate(accounts):
            matched_uid = "UNASSIGNED"
            for uid, prof in users.items():
                if (
                    prof.matches_identifier(acct.email)
                    or prof.matches_identifier(acct.player_name)
                    or prof.matches_identifier(acct.user_uuid)
                ):
                    matched_uid = f"Maps to -> [{uid}] ({prof.display_name})"
                    break
            active_tag = " [ACTIVE]" if (active and acct.email == active.email) else ""
            print(f"  {idx + 1}. {acct.summary()}{active_tag}  -->  {matched_uid}")
    print()


def reprocess_unassigned_files() -> None:
    """
    Moves held .fit files from Archive/unassigned/ back into the workout directory and runs sync.
    """
    workout_dir = get_fitfile_location()
    if not workout_dir:
        logger.error("Cannot reprocess unassigned files: Workout directory not found.")
        return
    archive_dir = get_archive_location(workout_dir)
    unassigned_dir = archive_dir / "unassigned"
    if not unassigned_dir.is_dir():
        print(f"No unassigned directory found at '{unassigned_dir}'.")
        return

    files = list(unassigned_dir.glob("*.fit"))
    if not files:
        print(f"No unassigned .fit files waiting in '{unassigned_dir}'.")
        return

    moved = 0
    for f in files:
        dest = workout_dir / f.name
        if dest.exists():
            dest = workout_dir / f"reprocess_{f.name}"
        shutil.move(str(f), str(dest))
        moved += 1
    logger.info(f"Moved {moved} file(s) from '{unassigned_dir}' back to '{workout_dir}' for reprocessing.")
    main()


def fix_recent_activity(target_activity_id: Optional[int] = None, user_id: str = "andrew") -> bool:
    """
    Directly enriches an existing Garmin Connect activity with workout title & MyWhooshInfo details.
    Defaults to today's activity 24705575099 or the latest ride from the catalog.
    """
    users, _ = load_users_config()
    athlete = users.get(user_id.lower()) or next(iter(users.values()), None)

    act_id = target_activity_id or 24705575099
    name = "Endurance into Max Aerobic"
    slug = "endurance-into-max-aerobic"
    np_p = 144
    if_val = 0.96
    tss = 95.1
    work = 482

    # If GIT_BACKUP_REPO_PATH is available, try reading entry from rides_index.json
    if GIT_BACKUP_REPO_PATH:
        base_repo = Path(GIT_BACKUP_REPO_PATH).expanduser().resolve()
        sub = athlete.archive_subdir if athlete else "andrew"
        j_path = base_repo / sub / "rides_index.json"
        if not j_path.exists():
            j_path = base_repo / "rides_index.json"
        if j_path.exists():
            try:
                with open(j_path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                    if isinstance(entries, list) and entries:
                        match = None
                        if target_activity_id:
                            for e in entries:
                                if str(e.get("ActivityID")) == str(target_activity_id):
                                    match = e
                                    break
                        if not match:
                            match = entries[-1]
                        if match:
                            act_id = int(match.get("ActivityID", act_id))
                            name = match.get("Workout Name", name)
                            slug = generate_workout_slug(name)
                            np_p = match.get("NP (W)", np_p) or np_p
                            if_val = match.get("IF", if_val) or if_val
                            tss = match.get("TSS", tss) or tss
                            work = match.get("Work (kJ)", work) or work
            except Exception as e:
                logger.debug(f"Could not load rides_index.json: {e}")

    logger.info(f"Connecting to Garmin to update activity {act_id} ('{name}')...")
    if not authenticate_to_garmin(athlete):
        logger.error("Authentication to Garmin Connect failed.")
        return False

    success = update_garmin_activity_metadata(
        activity_id=act_id,
        workout_name=name,
        slug=slug,
        np_val=np_p,
        if_val=if_val,
        tss_val=tss,
        work_kj=work,
        append_url=APPEND_MYWHOOSHINFO_URL,
    )
    if success:
        print(f"\n[SUCCESS] Garmin activity {act_id} has been updated:")
        print(f"  Title: {name}")
        print(f"  Notes: Workout: {name}")
        print(f"         Details: https://mywhooshinfo.com/workouts/workout/{slug}")
        print(f"         Metrics: NP: {np_p}W | IF: {if_val} | TSS: {tss} | Work: {work}kJ\n")
    return success


def _enable_user_garmin_in_config(user_id: str) -> None:
    """Sets garmin_enabled = true for user_id in users.json if currently false."""
    if not USERS_CONFIG_PATH.exists():
        return
    try:
        with open(USERS_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        users_dict = data.get("users", {})
        if user_id in users_dict and not users_dict[user_id].get("garmin_enabled", False):
            users_dict[user_id]["garmin_enabled"] = True
            with open(USERS_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Automatically enabled 'garmin_enabled: true' for '{user_id}' in users.json.")
    except Exception as e:
        logger.debug(f"Could not update users.json for '{user_id}': {e}")


def backfill_garmin_for_user(user_id: str) -> None:
    """
    Scans MyWhooshFitnessData/<user_id>/rides_index.json for workouts that were archived
    while garmin_enabled was false (ActivityID == ""), uploads them to the user's Garmin Connect
    account, enriches their metadata, and updates rides_index.json and rides_summary.csv.
    """
    users_map, _ = load_users_config()
    uid = user_id.strip().lower()
    if uid not in users_map:
        logger.error(f"User '{uid}' not found in users.json. Available: {list(users_map.keys())}")
        return

    athlete = users_map[uid]
    if not GIT_BACKUP_REPO_PATH:
        logger.error("GIT_BACKUP_REPO_PATH is not configured in .env; cannot locate archived workouts to backfill.")
        return

    repo_path = Path(GIT_BACKUP_REPO_PATH).expanduser().resolve()
    user_dir = repo_path / (athlete.archive_subdir or athlete.user_id)
    json_path = user_dir / "rides_index.json"
    if not json_path.is_file():
        logger.info(f"No archived workouts index found at '{json_path}'. Nothing to backfill.")
        return

    try:
        entries = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to read '{json_path}': {e}")
        return

    pending_entries = [e for e in entries if isinstance(e, dict) and not str(e.get("ActivityID") or "").strip()]
    if not pending_entries:
        logger.info(f"All archived workouts for '{athlete.display_name}' ({uid}) already have Garmin Activity IDs.")
        return

    logger.info(f"Found {len(pending_entries)} archived workout(s) for '{athlete.display_name}' without Garmin Activity IDs.")
    if not authenticate_to_garmin(athlete):
        logger.error(f"Garmin authentication failed for '{athlete.display_name}'. Aborting backfill.")
        return

    updated_any = False
    for entry in pending_entries:
        fit_fname = entry.get("Fit Filename", "")
        date_str = entry.get("Date", "")
        sport_str = entry.get("Sport", "Cycling")
        if not fit_fname or not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        fit_candidate = user_dir / sport_str.lower() / dt.strftime("%Y") / dt.strftime("%m") / fit_fname
        if not fit_candidate.is_file():
            matches = list(user_dir.rglob(fit_fname))
            non_raw = [m for m in matches if "raw" not in m.parts]
            fit_candidate = non_raw[0] if non_raw else (matches[0] if matches else fit_candidate)

        if not fit_candidate.is_file():
            logger.warning(f"Could not find archived FIT file '{fit_fname}' in '{user_dir}'. Skipping.")
            continue

        ok, act_id = upload_fit_file_to_garmin(fit_candidate, expected_dt=dt)
        if ok and act_id:
            w_name = entry.get("Workout Name", "")
            if UPDATE_GARMIN_METADATA:
                update_garmin_activity_metadata(
                    activity_id=act_id,
                    workout_name=w_name,
                    slug=generate_workout_slug(w_name),
                    np_val=int(entry.get("NP (W)") or 0),
                    if_val=float(entry.get("IF") or 0.0),
                    tss_val=float(entry.get("TSS") or 0.0),
                    work_kj=int(entry.get("Work (kJ)") or 0),
                    append_url=APPEND_MYWHOOSHINFO_URL,
                )
            telem = RideTelemetry(
                date_str=date_str,
                activity_id=str(act_id),
                sport=sport_str,
                workout_name=w_name,
                mywhooshinfo_url=entry.get("MyWhooshInfo URL", ""),
                duration_sec=int(entry.get("Duration (s)") or 0),
                distance_km=float(entry.get("Distance (km)") or 0.0),
                avg_power=int(entry.get("Avg Power (W)") or 0),
                np_power=int(entry.get("NP (W)") or 0),
                max_power=int(entry.get("Max Power (W)") or 0),
                avg_cadence=int(entry.get("Avg Cadence (RPM)") or 0),
                avg_hr=int(entry.get("Avg HR (BPM)") or 0),
                tss=float(entry.get("TSS") or 0.0),
                intensity_factor=float(entry.get("IF") or 0.0),
                work_kj=int(entry.get("Work (kJ)") or 0),
                fit_filename=fit_fname,
                year=dt.strftime("%Y"),
                month=dt.strftime("%m"),
                athlete_id=uid,
            )
            update_rides_catalog(user_dir, telem)
            updated_any = True

    if updated_any and ENABLE_GIT_BACKUP:
        git_backup_repository(
            repo_path=repo_path,
            workout_name="Backfill Garmin Activity IDs",
            ride_date_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            sport_name="backfill",
            auto_push=GIT_AUTO_PUSH,
            athlete=athlete,
        )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ("--fix-today", "--fix-latest", "--update-activity"):
            act_id = None
            if len(sys.argv) > 2 and sys.argv[2].isdigit():
                act_id = int(sys.argv[2])
            fix_recent_activity(act_id)
        elif cmd in ("--detect-user", "--list-users", "--users"):
            print_user_detection_report()
        elif cmd == "--auth":
            users_map, _ = load_users_config()
            target_uid = sys.argv[2].strip().lower() if len(sys.argv) > 2 else "andrew"
            if target_uid not in users_map:
                print(f"[ERROR] User '{target_uid}' not found in users.json. Available: {list(users_map.keys())}")
                sys.exit(1)
            if authenticate_to_garmin(users_map[target_uid]):
                _enable_user_garmin_in_config(target_uid)
                backfill_garmin_for_user(target_uid)
        elif cmd == "--backfill-garmin":
            target_uid = sys.argv[2].strip().lower() if len(sys.argv) > 2 else "monty"
            backfill_garmin_for_user(target_uid)
        elif cmd == "--reprocess-unassigned":
            reprocess_unassigned_files()
        elif cmd == "--migrate-archive":
            target_repo = sys.argv[2] if len(sys.argv) > 2 else GIT_BACKUP_REPO_PATH
            if not target_repo:
                print("[ERROR] Specify repository path or set GIT_BACKUP_REPO_PATH in .env.")
                sys.exit(1)
            rp = Path(target_repo).expanduser().resolve()
            if migrate_legacy_backup_repo(rp, default_user_subdir="andrew"):
                git_backup_repository(rp, "Migrate root archive into andrew/", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "archive", auto_push=GIT_AUTO_PUSH)
        else:
            main()
    else:
        main()

