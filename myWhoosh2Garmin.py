#!/usr/bin/env python3
"""
Script name: myWhoosh2Garmin.py
Usage: python myWhoosh2Garmin.py
Description:
    1. Scans for MyWhoosh workout .fit files.
    2. Performs device spoofing so Garmin Connect recognizes the device as a 'Garmin Edge 1030 Plus'.
    3. Calculates average cadence, power, heart rate if missing, and strips unwanted fields.
    4. Supports multi-user profile filtering (via MYWHOOSH_PROFILE_NAME in .env).
    5. Authenticates and uploads to Garmin Connect using Garth.
    6. Moves processed/uploaded .fit files to an 'Archive' directory to prevent duplicate uploads.
    7. Logs all events, successes, errors, and auth failures to sync.log.
"""

import os
import sys
import shutil
import logging
from pathlib import Path
import warnings
from datetime import datetime
from getpass import getpass
from typing import List, Optional

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
    from fit_tool.profile.profile_type import Manufacturer, GarminProduct
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
    from fit_tool.profile.messages.user_profile_message import UserProfileMessage
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

# Environment Configuration
def _get_env_clean(key: str, default: str = "") -> str:
    val = os.getenv(key, default) or ""
    return val.strip().strip("\"'")

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

    # 30-second rolling moving average
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


def append_value(values: list, message: object, field_name: str) -> None:
    """Appends field value from message if present and truthy, else 0."""
    value = getattr(message, field_name, None)
    values.append(value if value is not None else 0)


def extract_fit_profile_name(fit_file: FitFile) -> Optional[str]:
    """
    Extracts athlete/profile friendly name from FIT records if present.
    """
    for record in fit_file.records:
        msg = record.message
        if isinstance(msg, UserProfileMessage):
            name = getattr(msg, "friendly_name", None) or getattr(msg, "name", None)
            if name:
                return str(name).strip()
    return None


def extract_fit_ftp(fit_file: FitFile) -> Optional[int]:
    """
    Extracts configured FTP/threshold power from FIT records if present.
    """
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
) -> bool:
    """
    Parses, validates profile, calculates advanced cycling metrics (NP, IF, TSS, Work, FTP),
    cleans up metrics, and applies Garmin Edge 1030 Plus device spoofing.

    Returns:
        bool: True if processed successfully, False if skipped (e.g. mismatched profile) or on error.
    """
    try:
        fit_file = FitFile.from_file(str(input_fit_path))
    except Exception as e:
        logger.error(f"Failed to read FIT file {input_fit_path.name}: {e}")
        return False

    # Multi-User Profile Check
    if expected_profile:
        file_profile = extract_fit_profile_name(fit_file)
        if file_profile:
            if file_profile.lower() != expected_profile.lower():
                logger.warning(
                    f"Skipping '{input_fit_path.name}': Athlete profile '{file_profile}' "
                    f"does not match configured target profile '{expected_profile}'."
                )
                return False
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

    builder = FitFileBuilder()

    # Lap-level and Session-level metric accumulators
    lap_cadence_values: List[int] = []
    lap_power_values: List[int] = []
    lap_heart_rate_values: List[int] = []

    session_cadence_values: List[int] = []
    session_power_values: List[int] = []
    session_heart_rate_values: List[int] = []

    for record in fit_file.records:
        message = record.message

        # 1. Device Spoofing: File ID message
        if isinstance(message, FileIdMessage):
            try:
                message.manufacturer = Manufacturer.GARMIN.value
            except Exception:
                pass
            try:
                message.product = GarminProduct.EDGE_1030_PLUS.value
            except Exception:
                pass
            if hasattr(message, "garmin_product"):
                try:
                    message.garmin_product = GarminProduct.EDGE_1030_PLUS.value
                except Exception:
                    pass

        # 2. Device Spoofing: Device Info message
        elif isinstance(message, DeviceInfoMessage):
            try:
                message.manufacturer = Manufacturer.GARMIN.value
            except Exception:
                pass
            try:
                message.product = GarminProduct.EDGE_1030_PLUS.value
            except Exception:
                pass
            if hasattr(message, "garmin_product"):
                try:
                    message.garmin_product = GarminProduct.EDGE_1030_PLUS.value
                except Exception:
                    pass
            try:
                message.product_name = "Edge 1030 Plus"
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

            if p_val is not None:
                lap_power_values.append(p_val)
                session_power_values.append(p_val)
            if c_val is not None:
                lap_cadence_values.append(c_val)
                session_cadence_values.append(c_val)
            if hr_val is not None:
                lap_heart_rate_values.append(hr_val)
                session_heart_rate_values.append(hr_val)

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
                        lap_dur = getattr(message, "total_timer_time", None) or getattr(message, "total_elapsed_time", None) or len(lap_power_values)
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
                session_dur = getattr(message, "total_timer_time", None) or getattr(message, "total_elapsed_time", None) or len(session_power_values)
                try:
                    message.total_work = calculate_total_work_joules(session_power_values, session_dur)
                except Exception:
                    pass

                # Advanced Metrics: IF, TSS, Threshold Power (FTP)
                if effective_ftp > 0:
                    try:
                        message.threshold_power = effective_ftp
                    except Exception:
                        pass
                    
                    if_val = calculate_intensity_factor(np_val or calculate_avg(session_power_values), effective_ftp)
                    try:
                        message.intensity_factor = if_val
                    except Exception:
                        pass

                    tss_val = calculate_tss(session_dur, np_val or calculate_avg(session_power_values), if_val, effective_ftp)
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
        logger.info(f"Successfully processed and spoofed '{input_fit_path.name}' -> Garmin Edge 1030 Plus.")
        return True
    except Exception as e:
        logger.error(f"Failed to write processed FIT file {output_fit_path.name}: {e}")
        return False



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


def upload_fit_file_to_garmin(file_path: Path) -> bool:
    """
    Uploads a .fit file to Garmin Connect using Garth.
    Returns True if uploaded successfully or recognized as duplicate on Garmin.
    """
    try:
        with open(file_path, "rb") as f:
            uploaded = garth.client.upload(f)
            logger.info(f"Successfully uploaded {file_path.name} to Garmin Connect! (Response: {uploaded})")
            return True
    except GarthHTTPError as e:
        # HTTP 409 or duplicate activity error
        if "409" in str(e) or "duplicate" in str(e).lower():
            logger.warning(f"Activity {file_path.name} already exists on Garmin Connect (duplicate detected).")
            return True
        err_detail = ""
        if hasattr(e, "error") and hasattr(e.error, "response") and e.error.response is not None:
            err_detail = f" | Details: {e.error.response.text}"
        logger.error(f"Garmin HTTP upload error for {file_path.name}: {e}{err_detail}")
        return False
    except Exception as e:
        logger.error(f"Garmin upload failed for {file_path.name}: {e}")
        return False


def get_pending_fit_files(workout_dir: Path, archive_dir: Path) -> List[Path]:
    """
    Discovers all candidate .fit files in the workout directory, ignoring the archive directory.
    """
    candidate_files = []
    for f in workout_dir.glob("*.fit"):
        # Skip files already inside archive or subdirectories
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

            # Process, check athlete profile, and spoof as Garmin Edge 1030 Plus
            processed = process_and_spoof_fit_file(
                input_fit_path=fit_path,
                output_fit_path=temp_output,
                expected_profile=MYWHOOSH_PROFILE_NAME if MYWHOOSH_PROFILE_NAME else None,
            )


            if not processed:
                # File was skipped due to profile mismatch or parsing failure
                if temp_output.exists():
                    temp_output.unlink(missing_ok=True)
                continue

            # Upload to Garmin Connect
            upload_success = upload_fit_file_to_garmin(temp_output)

            # Clean up temp file
            if temp_output.exists():
                temp_output.unlink(missing_ok=True)

            # Archive the original workout file if successfully uploaded (or duplicate)
            if upload_success:
                archive_file(fit_path, archive_dir)
                success_count += 1
            else:
                logger.warning(f"File '{fit_path.name}' was not archived because upload failed.")

    finally:
        # Clean up temp directory
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    logger.info(f"=== Sync Completed: {success_count} file(s) synced and archived. ===")


if __name__ == "__main__":
    main()
