# Project Context & Historic Activity Data Rules

## Historic Ride & Fitness Telemetry Repository
- **Repository Location:** `C:\Users\Andrew\Documents\github\MyWhooshFitnessData`
- **Purpose:** Centralized, multi-user backup repository for all MyWhoosh FIT files and telemetry indexes (`rides_summary.csv` and `rides_index.json`).
- **Data Analysis & Machine Calibration Workflow:**
  - Whenever asked to analyze historic ride/workout data or perform machine/trainer resistance calibration, inspect `C:\Users\Andrew\Documents\github\MyWhooshFitnessData`.
  - **Always run `git pull` (or ensure the latest commits are fetched)** in `C:\Users\Andrew\Documents\github\MyWhooshFitnessData` before loading and evaluating historic data.
  - Per-User Top-Level Directories (e.g. `andrew/`, `<user2>/`):
    - Cycling: `C:\Users\Andrew\Documents\github\MyWhooshFitnessData\<user>\cycling\YYYY\MM\`
    - Rowing: `C:\Users\Andrew\Documents\github\MyWhooshFitnessData\<user>\rowing\YYYY\MM\`
    - Raw Untouched FIT Extracts: `C:\Users\Andrew\Documents\github\MyWhooshFitnessData\<user>\raw\<sport>\YYYY\MM\`
    - Index & Summaries: `C:\Users\Andrew\Documents\github\MyWhooshFitnessData\<user>\rides_summary.csv` and `rides_index.json`
