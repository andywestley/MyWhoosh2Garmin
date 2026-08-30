<h1 align="center" id="title">MyWhoosh2Garmin</h1>

<p align="center">
  <b>Automated, silent sync between MyWhoosh and Garmin Connect with Garmin Edge 1030 Plus spoofing and multi-user support.</b>
</p>

---

## 🧐 Features

* **Device Spoofing:** Spoofs the device in the `.fit` file to a **Garmin Edge 1030 Plus** (Garmin manufacturer ID `1`, product ID `3570`). This unlocks Garmin Connect cycling dynamics, training effect, and training status calculations.
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

Open `.env` in Notepad or any text editor and fill in your details:

```env
# Garmin Connect Credentials
GARMIN_EMAIL=your_email@example.com
GARMIN_PASSWORD=your_secure_password

# Multi-User Filter (Optional)
# Enter your MyWhoosh athlete/display name.
# If set, activities belonging to other profiles will be skipped.
MYWHOOSH_PROFILE_NAME=YourAthleteName

# Path Overrides (Optional - leave blank for auto-detection)
MYWHOOSH_WORKOUTS_DIR=
ARCHIVE_DIR=
```

### 5. Run an Initial Test

Run the Python script once manually from the command prompt to verify your credentials and path discovery:

```cmd
python myWhoosh2Garmin.py
```

Check `sync.log` to confirm successful execution:

```cmd
type sync.log
```

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
├── myWhoosh2Garmin.py    # Main synchronization and FIT spoofing engine
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
* License: GPL-3.0
