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
import subprocess
from datetime import datetime, timezone
from getpass import getpass
from typing import List, Optional, Tuple, Any, Dict
from dataclasses import dataclass, asdict

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
    """Cleans up a candidate workout name, ignoring generic or empty titles."""
    if not name:
        return None
    val = str(name).strip()
    if not val or val.lower() in GENERIC_ACTIVITY_NAMES:
        return None
    return val


def extract_workout_name(fit_file: FitFile, fallback_stem: str, ride_date_str: str) -> str:
    """
    Extracts workout title from FIT records:
    1. WorkoutMessage.workout_name / WorkoutMessage.name
    2. General message field scan for workout_name / wkt_name / workout_title / program_name
    3. SessionMessage.name / SessionMessage.sport_profile_name
    4. CourseMessage.name
    5. FileIdMessage.name
    6. Fallback to formatted filename stem or date string
    """
    # 1. WorkoutMessage
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, WorkoutMessage):
            w_name = clean_extracted_name(getattr(msg, "workout_name", None)) or clean_extracted_name(
                getattr(msg, "name", None)
            )
            if w_name:
                return w_name

    # 2. General field scan across all message records
    for record in fit_file.records:
        msg = record.message
        for attr in ("workout_name", "wkt_name", "workout_title", "program_name"):
            if hasattr(msg, attr):
                val = clean_extracted_name(getattr(msg, attr, None))
                if val:
                    return val

    # 3. SessionMessage
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, SessionMessage):
            s_name = clean_extracted_name(getattr(msg, "name", None)) or clean_extracted_name(
                getattr(msg, "sport_profile_name", None)
            )
            if s_name:
                return s_name

    # 4. CourseMessage
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, CourseMessage):
            c_name = clean_extracted_name(getattr(msg, "name", None))
            if c_name:
                return c_name

    # 5. FileIdMessage
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, FileIdMessage):
            f_name = clean_extracted_name(getattr(msg, "name", None))
            if f_name:
                return f_name

    # 6. Fallback from filename stem or date
    if fallback_stem:
        if re.match(r"^\d{4}[-_]\d{2}[-_]\d{2}", fallback_stem):
            return f"MyWhoosh Ride ({ride_date_str})"
        cleaned_stem = re.sub(r"[-_]+", " ", fallback_stem).strip()
        if cleaned_stem:
            return cleaned_stem.title()

    return f"MyWhoosh Ride ({ride_date_str})"


def generate_workout_slug(workout_name: str) -> str:
    """
    Generates a normalized URL slug for MyWhooshInfo from workout name:
    e.g. 'Spiked Aerobic #1' -> 'spiked-aerobic-1', 'Into the Red!' -> 'into-the-red'
    """
    if not workout_name:
        return ""
    slug = workout_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def get_mywhooshinfo_url(workout_name: str) -> str:
    """
    Constructs the MyWhooshInfo workout details URL.
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


def authenticate_to_garmin() -> bool:
    """
    Authenticates to Garmin Connect using stored tokens or .env credentials.
    """
    # Try resuming existing token session
    if TOKENS_PATH.exists():
        try:
            garth.resume(str(TOKENS_PATH))
            username = getattr(garth.client, "username", None) or "User"
            logger.info(f"Successfully resumed Garmin session for: {username}")
            return True
        except (GarthException, Exception) as e:
            logger.warning(f"Saved Garmin session token expired or invalid ({e}). Re-authenticating...")

    # Authenticate with credentials from .env
    if GARMIN_EMAIL and GARMIN_PASSWORD:
        logger.info(f"Authenticating to Garmin Connect using credentials for: {GARMIN_EMAIL}")
        try:
            garth.login(GARMIN_EMAIL, GARMIN_PASSWORD)
            garth.save(str(TOKENS_PATH))
            logger.info("Successfully authenticated and saved Garmin session tokens.")
            return True
        except GarthHTTPError as e:
            logger.error(f"Authentication failed: Invalid Garmin credentials in .env ({e}).")
            return False
        except Exception as e:
            logger.error(f"Unexpected authentication error: {e}")
            return False

    # Interactive Fallback if run manually from console
    if sys.stdin.isatty():
        logger.info("No credentials in .env. Prompting for interactive login...")
        try:
            username = input("Garmin Username/Email: ").strip()
            password = getpass("Garmin Password: ")
            garth.login(username, password)
            garth.save(str(TOKENS_PATH))
            logger.info("Successfully authenticated via interactive prompt.")
            return True
        except Exception as e:
            logger.error(f"Interactive authentication failed: {e}")
            return False

    logger.error("Garmin authentication failed: No valid session token or .env credentials found.")
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
    input_fit_path: Path, output_fit_path: Path, expected_profile: Optional[str] = None
) -> Tuple[bool, Optional[RideTelemetry]]:
    """
    Parses, validates profile, calculates advanced cycling metrics (NP, IF, TSS, Work, FTP),
    cleans up metrics, applies Garmin Edge 1030 Plus device spoofing, and extracts full ride telemetry.

    Returns:
        (bool, Optional[RideTelemetry]): (True, telemetry) if successful, (False, None) otherwise.
    """
    try:
        fit_file = FitFile.from_file(str(input_fit_path))
    except Exception as e:
        logger.error(f"Failed to read FIT file {input_fit_path.name}: {e}")
        return False, None

    # Multi-User Profile Check
    if expected_profile:
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

    # Determine FTP: Extract from FIT file if present, else fallback to ATHLETE_FTP from .env
    file_ftp = extract_fit_ftp(fit_file)
    effective_ftp = file_ftp if file_ftp and file_ftp > 0 else ATHLETE_FTP
    if effective_ftp > 0:
        logger.info(f"Using FTP: {effective_ftp}W ({'from FIT file' if file_ftp else 'from .env'})")
    else:
        logger.warning("No FTP configured or found in FIT file. Set ATHLETE_FTP in .env for TSS/IF calculations.")

    # Detect Sport & Dynamic Device Spoofing (e.g. Edge 1030 Plus for Cycling, Fenix 7 for Rowing)
    sport_name, sport_dir, spoof_product_id, spoof_product_name = extract_fit_sport(fit_file)
    logger.info(f"Detected Activity Sport: '{sport_name}' -> Spoofing as Garmin {spoof_product_name}")

    # Extract Ride Start Date & Workout Title
    ride_dt = extract_fit_timestamp(fit_file, fallback_mtime=input_fit_path.stat().st_mtime)
    ride_date_str = ride_dt.strftime("%Y-%m-%d %H:%M:%S")
    workout_title = extract_workout_name(fit_file, input_fit_path.stem, ride_date_str)
    mywhooshinfo_url = get_mywhooshinfo_url(workout_title)

    logger.info(f"Extracted Workout Title: '{workout_title}' | Slug URL: {mywhooshinfo_url}")

    builder = FitFileBuilder()

    # Lap-level and Session-level metric accumulators
    lap_cadence_values: List[int] = []
    lap_power_values: List[int] = []
    lap_heart_rate_values: List[int] = []

    session_cadence_values: List[int] = []
    session_power_values: List[int] = []
    session_heart_rate_values: List[int] = []

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

        # 5. Lap Message: Populate missing averages & power metrics
        elif isinstance(message, LapMessage):
            message = clone_message(message, LapMessage)
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

        # 6. Session Message: Populate missing averages, NP, IF, TSS, Work, and FTP
        elif isinstance(message, SessionMessage):
            message = clone_message(message, SessionMessage)
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
    )

    return True, telemetry


def archive_file(source_file: Path, archive_dir: Path) -> Optional[Path]:
    """
    Moves a processed file into the archive folder, handling filename collisions.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / source_file.name

    if destination.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = archive_dir / f"{source_file.stem}_{timestamp}{source_file.suffix}"

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
    lines = [f"Workout: {workout_name}"]
    if append_url and slug:
        lines.append(f"Details: https://mywhooshinfo.com/workouts/workout/{slug}")
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
                    if (
                        (telemetry.fit_filename and r.get("Fit Filename") == telemetry.fit_filename)
                        or (telemetry.activity_id and r.get("ActivityID") == str(telemetry.activity_id))
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
            (telemetry.fit_filename and entry.get("Fit Filename") == telemetry.fit_filename)
            or (telemetry.activity_id and entry.get("ActivityID") == str(telemetry.activity_id))
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

    commit_msg = f"Auto-backup {sport_name.lower()}: {workout_name} ({ride_date_str})"

    # 1. git pull (to sync any remote deletions or edits from GitHub)
    try:
        subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as e:
        logger.debug(f"git pull skipped or failed in {repo_path}: {e}")

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
    git_env = os.environ.copy()
    if "GIT_AUTHOR_NAME" not in git_env or not git_env.get("GIT_AUTHOR_NAME"):
        git_env["GIT_AUTHOR_NAME"] = MYWHOOSH_PROFILE_NAME or "MyWhoosh Sync"
    if "GIT_AUTHOR_EMAIL" not in git_env or not git_env.get("GIT_AUTHOR_EMAIL"):
        git_env["GIT_AUTHOR_EMAIL"] = GARMIN_EMAIL or "sync@mywhoosh2garmin.local"
    if "GIT_COMMITTER_NAME" not in git_env or not git_env.get("GIT_COMMITTER_NAME"):
        git_env["GIT_COMMITTER_NAME"] = MYWHOOSH_PROFILE_NAME or "MyWhoosh Sync"
    if "GIT_COMMITTER_EMAIL" not in git_env or not git_env.get("GIT_COMMITTER_EMAIL"):
        git_env["GIT_COMMITTER_EMAIL"] = GARMIN_EMAIL or "sync@mywhoosh2garmin.local"

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

    # 4. git push
    if auto_push:
        try:
            push_res = subprocess.run(
                ["git", "push"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if push_res.returncode != 0:
                logger.warning(
                    f"git push failed in {repo_path} (offline or remote rejected): {push_res.stderr.strip()}"
                )
                return False
            logger.info("Successfully pushed ride backup to remote git repository.")
        except Exception as e:
            logger.warning(f"Failed to execute git push in {repo_path}: {e}")
            return False

    return True


def backup_and_index_ride(
    source_fit_path: Path,
    telemetry: RideTelemetry,
    repo_path_str: str,
    subdir_str: str,
    auto_push: bool = True,
) -> bool:
    """
    Copies FIT file to structured year/month folder in the backup repo, updates catalog, and commits.
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

    # Target folder:
    # If subdir_str == "auto": use telemetry.sport.lower() (e.g. 'cycling', 'rowing')
    # If subdir_str is custom: use that
    # If subdir_str is empty/blank: use repo_path directly
    target_dir = repo_path
    if subdir_str and subdir_str.lower() == "auto":
        target_dir = target_dir / telemetry.sport.lower()
    elif subdir_str:
        target_dir = target_dir / subdir_str

    target_dir = target_dir / telemetry.year / telemetry.month

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_fit = target_dir / source_fit_path.name
        shutil.copy2(str(source_fit_path), str(dest_fit))
        logger.info(f"Backed up ride FIT file to '{dest_fit}'.")
    except Exception as e:
        logger.warning(f"Failed to copy FIT file to backup directory '{target_dir}': {e}")
        return False

    # Update rides_summary.csv and rides_index.json
    try:
        update_rides_catalog(repo_path, telemetry)
    except Exception as e:
        logger.warning(f"Failed to update ride catalog in '{repo_path}': {e}")

    # Automated git commit & push
    try:
        git_backup_repository(
            repo_path=repo_path,
            workout_name=telemetry.workout_name,
            ride_date_str=telemetry.date_str,
            sport_name=telemetry.sport,
            auto_push=auto_push,
        )
    except Exception as e:
        logger.warning(f"Git auto-backup encountered an error: {e}")

    return True



def get_pending_fit_files(workout_dir: Path, archive_dir: Path) -> List[Path]:
    """
    Discovers all candidate .fit files in the workout directory, ignoring the archive directory.
    """
    candidate_files = []
    for f in workout_dir.glob("*.fit"):
        if f.is_file() and archive_dir not in f.parents and f.parent == workout_dir:
            candidate_files.append(f)

    # Sort oldest to newest by modification time
    candidate_files.sort(key=lambda x: x.stat().st_mtime)
    return candidate_files


def main():
    """Main execution entry point."""
    logger.info("=== Starting MyWhoosh2Garmin Sync ===")

    # 1. Discover Workout Path
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

    # 2. Authenticate to Garmin
    if not authenticate_to_garmin():
        logger.error("Aborting sync: Authentication with Garmin Connect failed.")
        sys.exit(1)

    # 3. Temporary processing directory
    temp_dir = SCRIPT_DIR / "temp_processing"
    temp_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0

    try:
        for fit_path in pending_files:
            logger.info(f"Processing '{fit_path.name}'...")
            temp_output = temp_dir / fit_path.name

            # Process, check athlete profile, extract telemetry, and spoof as Garmin Edge 1030 Plus
            processed, telemetry = process_and_spoof_fit_file(
                input_fit_path=fit_path,
                output_fit_path=temp_output,
                expected_profile=MYWHOOSH_PROFILE_NAME if MYWHOOSH_PROFILE_NAME else None,
            )

            if not processed or telemetry is None:
                # File was skipped due to profile mismatch or parsing failure
                if temp_output.exists():
                    temp_output.unlink(missing_ok=True)
                continue

            # Upload to Garmin Connect
            ride_dt_obj = datetime.strptime(telemetry.date_str, "%Y-%m-%d %H:%M:%S")
            upload_success, activity_id = upload_fit_file_to_garmin(temp_output, expected_dt=ride_dt_obj)

            if upload_success:
                if activity_id:
                    telemetry.activity_id = str(activity_id)

                # Enrich Garmin Activity metadata (Title, description with MyWhooshInfo and metrics)
                if UPDATE_GARMIN_METADATA and activity_id:
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

                # Automated GitHub Ride Backup & Telemetry Indexing
                if ENABLE_GIT_BACKUP:
                    if GIT_BACKUP_REPO_PATH:
                        backup_file_target = temp_output if temp_output.exists() else fit_path
                        backup_and_index_ride(
                            source_fit_path=backup_file_target,
                            telemetry=telemetry,
                            repo_path_str=GIT_BACKUP_REPO_PATH,
                            subdir_str=GIT_BACKUP_SUBDIR,
                            auto_push=GIT_AUTO_PUSH,
                        )
                    else:
                        logger.warning("GitHub backup is enabled (ENABLE_GIT_BACKUP=true), but GIT_BACKUP_REPO_PATH is empty in .env. Skipping Git backup.")
                else:
                    logger.info("GitHub ride backup is disabled (ENABLE_GIT_BACKUP=false).")

                # Clean up temp file
                if temp_output.exists():
                    temp_output.unlink(missing_ok=True)

                # Archive original workout file
                archive_file(fit_path, archive_dir)
                success_count += 1
            else:
                logger.warning(f"File '{fit_path.name}' was not archived because upload failed.")
                if temp_output.exists():
                    temp_output.unlink(missing_ok=True)

    finally:
        # Clean up temp directory
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    logger.info(f"=== Sync Completed: {success_count} file(s) synced and archived. ===")


if __name__ == "__main__":
    main()
