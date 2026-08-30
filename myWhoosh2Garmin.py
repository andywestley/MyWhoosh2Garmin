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


def append_value(values: list, message: object, field_name: str) -> None:
    """Appends field value from message if present and truthy, else 0."""
    value = getattr(message, field_name, None)
    values.append(value if value is not None else 0)


def reset_values() -> tuple[list, list, list, list]:
    """Resets metric accumulators."""
    return [], [], [], []


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


def safe_update_session_message(
    orig_msg: SessionMessage, avg_cadence: int, avg_power: int, avg_hr: int
) -> SessionMessage:
    """
    Safely copies fields from an existing SessionMessage to a new mutable SessionMessage
    and fills in calculated average cadence, power, and heart rate if not present.
    """
    new_msg = SessionMessage()
    for fld in orig_msg.fields:
        if fld.is_valid():
            try:
                new_msg.get_field(fld.id).set_values(fld.get_values())
            except Exception:
                pass

    if not getattr(new_msg, "avg_cadence", None) and avg_cadence:
        try:
            new_msg.avg_cadence = avg_cadence
        except Exception:
            pass

    if not getattr(new_msg, "avg_power", None) and avg_power:
        try:
            new_msg.avg_power = avg_power
        except Exception:
            pass

    if not getattr(new_msg, "avg_heart_rate", None) and avg_hr:
        try:
            new_msg.avg_heart_rate = avg_hr
        except Exception:
            pass

    return new_msg


def process_and_spoof_fit_file(
    input_fit_path: Path, output_fit_path: Path, expected_profile: Optional[str] = None
) -> bool:
    """
    Parses, validates profile, cleans up metrics, and applies Garmin Edge 1030 Plus device spoofing.

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

    builder = FitFileBuilder()
    lap_values, cadence_values, power_values, heart_rate_values = reset_values()
    has_device_info = False

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


        # 3. Lap Message Accumulation
        elif isinstance(message, LapMessage):
            append_value(lap_values, message, "start_time")
            append_value(lap_values, message, "total_elapsed_time")
            append_value(lap_values, message, "total_distance")
            append_value(lap_values, message, "avg_speed")
            append_value(lap_values, message, "max_speed")
            append_value(lap_values, message, "avg_heart_rate")
            append_value(lap_values, message, "max_heart_rate")
            append_value(lap_values, message, "avg_cadence")
            append_value(lap_values, message, "max_cadence")
            append_value(lap_values, message, "total_calories")

        # 4. Record Message: Remove temperature & accumulate metrics
        elif isinstance(message, RecordMessage):
            try:
                message.remove_field(RecordTemperatureField.ID)
            except Exception:
                pass
            append_value(cadence_values, message, "cadence")
            append_value(power_values, message, "power")
            append_value(heart_rate_values, message, "heart_rate")

        # 5. Session Message: Safely populate missing averages
        elif isinstance(message, SessionMessage):
            message = safe_update_session_message(
                orig_msg=message,
                avg_cadence=calculate_avg(cadence_values),
                avg_power=calculate_avg(power_values),
                avg_hr=calculate_avg(heart_rate_values),
            )
            lap_values, cadence_values, power_values, heart_rate_values = reset_values()

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
