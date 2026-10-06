#!/usr/bin/env python3
"""
Utility script to generate a valid dummy .fit activity file for end-to-end testing.
Creates a 1-minute indoor cycling workout named 'TEST DUMMY RIDE (DELETE ME)'.

Usage:
    python generate_test_fit.py
    python generate_test_fit.py --deposit   # Drops file directly into your MyWhoosh workouts folder
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime

try:
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
    from fit_tool.profile.messages.user_profile_message import UserProfileMessage
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.messages.event_message import EventMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.lap_message import LapMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.messages.activity_message import ActivityMessage
    from fit_tool.profile.profile_type import (
        FileType,
        Sport,
        SubSport,
        Manufacturer,
        Event,
        EventType,
        Activity,
        GarminProduct,
    )
except ImportError:
    print("Error: fit-tool library not found. Run 'pip install fit-tool'")
    sys.exit(1)

from myWhoosh2Garmin import get_fitfile_location, MYWHOOSH_PROFILE_NAME


def create_dummy_fit(output_path: Path, workout_name: str = "TEST DUMMY RIDE (DELETE ME)", duration_seconds: int = 60) -> Path:
    builder = FitFileBuilder()
    now_ms = int(datetime.now().timestamp() * 1000)
    total_dist = float(duration_seconds * 8.5)

    # 1. File ID Message
    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY.value
    file_id.manufacturer = Manufacturer.GARMIN.value
    file_id.product = GarminProduct.EDGE_1030_PLUS.value
    file_id.garmin_product = GarminProduct.EDGE_1030_PLUS.value
    file_id.time_created = now_ms
    file_id.serial_number = 999999
    builder.add(file_id)

    # 2. User Profile Message (Matches MYWHOOSH_PROFILE_NAME filter)
    if MYWHOOSH_PROFILE_NAME:
        user_prof = UserProfileMessage()
        user_prof.friendly_name = MYWHOOSH_PROFILE_NAME
        builder.add(user_prof)

    # 2. Device Info Message
    dev_info = DeviceInfoMessage()
    dev_info.timestamp = now_ms
    dev_info.manufacturer = Manufacturer.GARMIN.value
    dev_info.product = GarminProduct.EDGE_1030_PLUS.value
    dev_info.garmin_product = GarminProduct.EDGE_1030_PLUS.value
    dev_info.product_name = "Edge 1030 Plus"
    dev_info.serial_number = 999999
    builder.add(dev_info)

    # 3. Workout Message
    workout_msg = WorkoutMessage()
    workout_msg.workout_name = workout_name
    workout_msg.sport = Sport.CYCLING.value
    workout_msg.num_valid_steps = 1
    builder.add(workout_msg)

    # 4. Timer Start Event Message (Required by Garmin activity processor)
    timer_start = EventMessage()
    timer_start.timestamp = now_ms
    timer_start.event = Event.TIMER.value
    timer_start.event_type = EventType.START.value
    builder.add(timer_start)

    # 5. 1-second interval Records
    for i in range(duration_seconds):
        rec = RecordMessage()
        rec.timestamp = now_ms + (i * 1000)
        rec.power = 160 + (i % 15)          # ~160-175 W
        rec.cadence = 85 + (i % 5)          # ~85-90 RPM
        rec.heart_rate = 135 + (i % 8)      # ~135-142 BPM
        rec.distance = float(i * 8.5)       # ~30.6 km/h
        rec.speed = 8.5
        builder.add(rec)

    # 6. Timer Stop Event Message (Required by Garmin activity processor)
    timer_stop = EventMessage()
    timer_stop.timestamp = now_ms + (duration_seconds * 1000)
    timer_stop.event = Event.TIMER.value
    timer_stop.event_type = EventType.STOP_ALL.value
    builder.add(timer_stop)

    # 7. Lap Message
    lap = LapMessage()
    lap.timestamp = now_ms + (duration_seconds * 1000)
    lap.start_time = now_ms
    lap.total_elapsed_time = float(duration_seconds)
    lap.total_timer_time = float(duration_seconds)
    lap.total_distance = total_dist
    lap.sport = Sport.CYCLING.value
    lap.sub_sport = SubSport.INDOOR_CYCLING.value
    builder.add(lap)

    # 8. Session Message
    session = SessionMessage()
    session.timestamp = now_ms + (duration_seconds * 1000)
    session.start_time = now_ms
    session.total_elapsed_time = float(duration_seconds)
    session.total_timer_time = float(duration_seconds)
    session.total_distance = total_dist
    session.sport = Sport.CYCLING.value
    session.sub_sport = SubSport.INDOOR_CYCLING.value
    session.sport_profile_name = workout_name
    session.name = workout_name
    builder.add(session)

    # 9. Activity Message
    act = ActivityMessage()
    act.timestamp = now_ms + (duration_seconds * 1000)
    act.total_timer_time = float(duration_seconds)
    act.num_sessions = 1
    act.type = Activity.MANUAL.value
    act.event = Event.ACTIVITY.value
    act.event_type = EventType.STOP.value
    builder.add(act)

    fit_file = builder.build()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fit_file.to_file(str(output_path))
    return output_path


def main():
    now_dt = datetime.now()
    default_name = f"TEST DUMMY RIDE ({now_dt.strftime('%Y-%m-%d %H:%M:%S')}) - DELETE ME"
    default_filename = f"dummy_test_ride_{now_dt.strftime('%Y%m%d_%H%M%S')}.fit"

    parser = argparse.ArgumentParser(description="Generate a dummy .fit file for Garmin upload testing.")
    parser.add_argument(
        "--deposit",
        action="store_true",
        help="Deposit the dummy .fit file directly into the MyWhoosh workouts directory so myWhoosh2Garmin picks it up.",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=default_name,
        help="Workout title to embed in the test FIT file.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Custom output file path.",
    )

    args = parser.parse_args()

    filename = default_filename

    if args.out:
        target_path = Path(args.out).resolve()
    elif args.deposit:
        workouts_dir = get_fitfile_location()
        if not workouts_dir:
            print("[ERROR] Could not detect MyWhoosh workouts folder. Specify MYWHOOSH_WORKOUTS_DIR in .env or use default output.")
            sys.exit(1)
        target_path = workouts_dir / filename
    else:
        target_path = Path(__file__).resolve().parent / filename

    created_path = create_dummy_fit(target_path, workout_name=args.name)
    print(f"[SUCCESS] Created test dummy FIT file:")
    print(f"          Path: {created_path}")
    print(f"          Title: '{args.name}'")
    print(f"          Duration: 60 seconds (with power, cadence, HR records)\n")
    if args.deposit:
        print("Now run: python myWhoosh2Garmin.py to process and upload it to Garmin Connect.")
    else:
        print("To test sync, you can move this file into your MyWhoosh workouts folder, or use:")
        print("  python generate_test_fit.py --deposit")
        print("  python myWhoosh2Garmin.py")


if __name__ == "__main__":
    main()
