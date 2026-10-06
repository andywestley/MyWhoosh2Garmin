<h1 align="center" id="title">MyWhoosh2Garmin</h1>

<p align="center">
  <b>Automated, silent sync between MyWhoosh and Garmin Connect with Garmin Edge 1030 Plus spoofing, workout metadata enrichment, and automated GitHub ride backup.</b>
</p>

---

## 🧐 Features

* **Multi-Sport & Dynamic Device Spoofing:** Automatically detects activity sport type from FIT records (`Sport.CYCLING` vs `Sport.ROWING` / `INDOOR_ROWING`):
  * **Cycling:** Spoofs as a **Garmin Edge 1030 Plus** (unlocks Garmin cycling dynamics, training effect, and status).
  * **Rowing:** Spoofs as a **Garmin Fēnix 7** (preserves stroke rates, 500m split metrics, and rowing training loads).
* **Advanced Metrics Injection:** Automatically calculates and injects **Normalized Power (NP®)**, **Intensity Factor (IF®)**, **Training Stress Score (TSS®)**, **Threshold Power (FTP)**, and **Total Work (kJ)** into session and lap summaries.
* **Workout Title Discovery & MyWhooshInfo Linkage:** Automatically extracts embedded workout names from FIT records and builds normalized links to [MyWhooshInfo](https://mywhooshinfo.com) workout profiles (e.g., `https://mywhooshinfo.com/workouts/workout/spiked-aerobic-1`).
* **Garmin Connect Activity Metadata Enrichment:** Updates the uploaded Garmin activity title with the workout name and enriches the description notes with Coggan metrics (`NP`, `IF`, `TSS`, `Work`) and direct MyWhooshInfo reference links.
* **Automated Multi-Sport GitHub Backup & Indexing:** Backs up `.fit` files to a configurable local Git repository partitioned by sport and date (`{repo}/cycling/YYYY/MM/` or `{repo}/rowing/YYYY/MM/`), maintains an append-only `rides_summary.csv` and `rides_index.json` catalog with sport classifications, and executes automated `git add`, `git commit`, and `git push`.
* **Automated Path Discovery:** Automatically searches `%LOCALAPPDATA%` (e.g. `AppData\Local\MyWhoosh\Saved\Workouts` and Microsoft Store package directories) without hardcoding paths.
* **Secure Credentials via `.env`:** Stores Garmin Connect credentials securely using `python-dotenv`. Saves session tokens in `.garth/` for fast recurring logins.
* **Duplicate Prevention & Archiving:** Automatically moves processed and uploaded `.fit` files to an `Archive/` subfolder so they are never processed twice.
* **Multi-User Profile Filtering:** If multiple athletes use the same machine/MyWhoosh install, you can specify `MYWHOOSH_PROFILE_NAME` in `.env` to prevent other people's workouts from being uploaded to your Garmin account.
* **Robust Logging:** Records all synchronization runs, authentication events, profile checks, and error messages to `sync.log`.
* **Silent Windows Background Execution:** Includes a `run_sync_silent.vbs` wrapper script for Windows Task Scheduler to run silently in the background without terminal pop-ups.

---

## 🛠️ Windows Deployment & Setup Guide

### 1. Prerequisites

* **Python 3.10+** installed on the target machine.
  > During Python installation, ensure you check **"Add Python to PATH"**.
* **Git** (or download and extract the repository ZIP).

### 2. Clone or Copy the Repository

```cmd
git clone https://github.com/andywestley/MyWhoosh2Garmin.git
cd MyWhoosh2Garmin
```

### 3. Install Dependencies

Install the required packages using the included `requirements.txt`:

```cmd
pip install -r requirements.txt
```

*(Dependencies: `garth`, `fit_tool`, `python-dotenv`)*

### 4. Configure `.env`

Copy `.env.example` to `.env`:

```cmd
copy .env.example .env
```

Open `.env` in Notepad or any text editor and configure your parameters:

```env
# Garmin Connect Credentials
GARMIN_EMAIL=your_email@example.com
GARMIN_PASSWORD=your_secure_password

# Athlete FTP (in Watts) - used for NP, IF, and TSS calculations
ATHLETE_FTP=150

# Multi-User Filter (Optional)
# Enter your MyWhoosh athlete/display name to filter workouts.
MYWHOOSH_PROFILE_NAME=YourAthleteName

# Path Overrides (Optional - leave blank for auto-detection)
MYWHOOSH_WORKOUTS_DIR=
ARCHIVE_DIR=

# --- Garmin Activity Metadata Enrichment ---
UPDATE_GARMIN_METADATA=true
APPEND_MYWHOOSHINFO_URL=true

# --- GitHub Repository Backup & Indexing ---
ENABLE_GIT_BACKUP=true
GIT_BACKUP_REPO_PATH="C:/Users/Andrew/Documents/github/York210-Controller"
GIT_BACKUP_SUBDIR="auto"
GIT_AUTO_PUSH=true
```

### 5. Configuration Options Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GARMIN_EMAIL` | _(required)_ | Garmin Connect username or email. |
| `GARMIN_PASSWORD` | _(required)_ | Garmin Connect account password. |
| `ATHLETE_FTP` | `150` | Functional Threshold Power in Watts for IF/TSS calculations. |
| `MYWHOOSH_PROFILE_NAME` | _(blank)_ | Athlete name filter. Skips files matching other athlete profiles. |
| `MYWHOOSH_WORKOUTS_DIR` | _(auto)_ | Custom path override for MyWhoosh workout directory. |
| `ARCHIVE_DIR` | _(auto)_ | Custom path override for processed file archive folder. |
| `UPDATE_GARMIN_METADATA` | `true` | Update Garmin activity title and description with workout details. |
| `APPEND_MYWHOOSHINFO_URL`| `true` | Include MyWhooshInfo workout link in Garmin activity description. |
| `ENABLE_GIT_BACKUP` | `true` | Enable automated `.fit` backup and telemetry cataloging in Git. |
| `GIT_BACKUP_REPO_PATH` | _(blank)_ | Path to local Git repository for ride/activity backups and index files. |
| `GIT_BACKUP_SUBDIR` | `auto` | Subdirectory inside repository: `auto` (routes to `cycling/` and `rowing/`), custom folder name (e.g. `rides`), or empty `""` for repo root. |
| `GIT_AUTO_PUSH` | `true` | Automatically run `git push` after committing activity backups. |


### 6. Test with a Dummy Activity (Optional)

You can generate a 60-second test activity `.fit` file titled `TEST DUMMY RIDE (DELETE ME)` with realistic power, heart rate, and cadence to test the entire upload, spoofing, and metadata pipeline:

```cmd
# Generate a test dummy .fit file in your MyWhoosh workouts folder
python generate_test_fit.py --deposit

# Run the sync script to process and upload it
python myWhoosh2Garmin.py
```

After verifying the upload in Garmin Connect, you can delete the activity in Garmin Connect with one click.

### 7. Run an Initial Test

Run the Python script once manually from the command prompt to verify credentials, upload, and backup:

```cmd
python myWhoosh2Garmin.py
```

Check `sync.log` to confirm successful execution:

```cmd
type sync.log
```

---

## 📊 Telemetry Indexing & Backup Structure

When `ENABLE_GIT_BACKUP=true` is set, rides are automatically archived into the target Git repository:

```
GIT_BACKUP_REPO_PATH/
├── rides/
│   └── 2026/
│       └── 10/
│           └── 2026-10-06-08-30-00.fit
├── rides_summary.csv
└── rides_index.json
```

### `rides_summary.csv` Schema
The catalog contains the following columns for easy spreadsheet and dashboard analysis:
* `Date`, `ActivityID`, `Workout Name`, `MyWhooshInfo URL`, `Duration (s)`, `Distance (km)`, `Avg Power (W)`, `NP (W)`, `Max Power (W)`, `Avg Cadence (RPM)`, `Avg HR (BPM)`, `TSS`, `IF`, `Work (kJ)`, `Fit Filename`

---

## 🕒 Setting Up Windows Task Scheduler (Silent Background Sync)

To run the sync automatically and silently after workouts:

### Option A: Recurring Schedule (e.g., Every 15 or 30 Minutes)

1. Open **Task Scheduler** (press `Win + R`, type `taskschd.msc`, and press Enter).
2. Click **Create Task...** in the right-hand panel.
3. **General Tab:**
   * **Name:** `MyWhoosh2Garmin Silent Sync`
   * **Security Options:** Select **"Run only when user is logged on"** (or "Run whether user is logged on or not").
4. **Triggers Tab:**
   * Click **New...**
   * **Begin the task:** *On a schedule* (e.g. Daily).
   * Check **"Repeat task every:"** and select `15 minutes` (or `30 minutes`) for a duration of `Indefinitely`.
5. **Actions Tab:**
   * Click **New...**
   * **Action:** *Start a program*
   * **Program/script:** `wscript.exe`
   * **Add arguments:** `"C:\path\to\MyWhoosh2Garmin\run_sync_silent.vbs"`
   * **Start in:** `C:\path\to\MyWhoosh2Garmin`
6. Click **OK** to save the task.

### Option B: Trigger When MyWhoosh Closes

You can also trigger a task when the `mywhoosh.exe` process terminates, or use the provided PowerShell helper script.

---

## 📁 Directory Structure

```
MyWhoosh2Garmin/
├── .env                  # Your private credentials & configuration (git-ignored)
├── .env.example          # Template configuration
├── .gitignore            # Git ignore rules for logs, tokens, and archives
├── myWhoosh2Garmin.py    # Main synchronization, enrichment, and backup engine
├── requirements.txt      # Python package dependencies
├── run_sync_silent.vbs   # Silent wrapper script for Task Scheduler
├── sync.log              # Rolling execution log file
└── Archive/              # Destination folder for processed/uploaded .fit files
```

---

## 🔍 How Multi-User Profile Detection Works

When MyWhoosh finishes an activity, it writes the athlete's friendly name to the `UserProfileMessage` in the `.fit` file.

* If `MYWHOOSH_PROFILE_NAME` is configured in `.env`, the script reads this message before modifying or uploading the file.
* If the name does not match the configured profile, the script logs a warning in `sync.log` and safely skips the file.
* If `MYWHOOSH_PROFILE_NAME` is blank, all `.fit` files in the directory are processed.

---

## 📜 Credits & License

* [Garth by matin](https://github.com/matin/garth) - Garmin Connect authentication and upload API.
* [fit_tool](https://pypi.org/project/fit-tool/) - FIT file parsing and binary manipulation.
* [MyWhooshInfo](https://mywhooshinfo.com) - Workout catalog and community database.
* License: GPL-3.0

