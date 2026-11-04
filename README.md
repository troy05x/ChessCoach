# AI Chess Coach

A self-hosted, lightweight AI Chess Coach Web Application engineered to run 24/7 on an Ubuntu 20.04/22.04 cloud instance (2 vCPUs, 1GB RAM).

The application automatically retrieves all chess matches played yesterday by **Troy05x** across **Chess.com** and **Lichess**, performs fast local positional blunder analysis using **Stockfish 16**, detects multi-day macro patterns (weekly and monthly recurring flaws), synthesizes Grandmaster-grade coaching reports using **Google Gemini 2.5 Flash**, and serves interactive dashboards on a mobile-responsive web server.

---

## Architecture Overview

```text
       Daily Cron (04:00 AM)
                 │
                 ▼
       ┌──────────────────┐
       │    engine.py     │
       └─────────┬────────┘
                 │
       ┌─────────┴─────────┐
       │                   │
       ▼                   ▼
┌──────────────┐    ┌──────────────┐
│  Chess.com   │    │   Lichess    │
│  Public API  │    │  Public API  │
└──────┬───────┘    └──────┬───────┘
       │                   │
       └─────────┬─────────┘
                 ▼
      ┌──────────────────────┐
      │  Stockfish 16 Engine │  (Bounded: 1 Thread, 16MB Hash, 0.03s/move)
      │  Centipawn Evaluation│  (User Troy05x mistakes >100cp, blunders >200cp)
      └──────────┬───────────┘
                 ▼
      ┌──────────────────────┐
      │ SQLite DB Telemetry  │  (/data/chess_coach.db)
      │ Rolling Macro Stats  │  (7-Day & 30-Day recurring flaws by phase & piece)
      └──────────┬───────────┘
                 ▼
      ┌──────────────────────┐
      │ Google Gemini Flash  │  (gemini-2.5-flash via google-genai)
      │ AI Coaching Prompt   │
      └──────────┬───────────┘
                 ▼
      ┌──────────────────────┐
      │ Daily Markdown Report│  (/data/reports/YYYY-MM-DD.md)
      └──────────┬───────────┘
                 │
                 ▼
       ┌──────────────────┐
       │      app.py      │  (Flask Web Dashboard, port 5000)
       │ systemd service  │  (chess-coach.service, memory-hardened)
       └──────────────────┘
```

---

## Features

1. **Automated Multi-Platform Ingestion:**
   - Automatically queries Chess.com monthly archives and Lichess games for user `Troy05x` for yesterday's date in UTC.
   - If zero games were played yesterday, logs a clean "No games" summary in the database and generates a resting report without wasting API tokens.

2. **Ultra-Low Memory Stockfish Evaluation:**
   - Strict resource boundaries configured: `Threads: 1`, `Hash: 16 MB`, `time_limit: 0.03s per move`.
   - Never exceeds RAM quotas or risks OOM (Out Of Memory) crashes on a 1GB RAM instance.
   - Specifically filters and evaluates moves played by `Troy05x`, calculating exact centipawn loss relative to the user:
     - **Inaccuracies / Mistakes:** Centipawn loss > 100
     - **Blunders:** Centipawn loss > 200

3. **Macro Pattern Recognition & Periodical Feedback:**
   - Rather than just focusing on isolated moves, tracks macro patterns across opening, middlegame, and endgame phases.
   - Identifies frequent culprit pieces involved in blunders.
   - Maintains rolling 7-day (weekly) and 30-day (monthly) pattern tracking inside SQLite to discover recurring psychological or positional flaws over time.

4. **AI Coaching via Gemini 2.5 Flash:**
   - Packages daily game telemetry and rolling weekly/monthly trends into a structured prompt sent to Google Gemini 2.5 Flash (`gemini-2.5-flash`) using the official `google-genai` SDK.
   - Produces structured reports with sections:
     - *Executive Performance Overview*
     - *Recurring Macro Patterns & Psychological Blindspots*
     - *Strategic Blindspots*
     - *Tactical Failures & Blunder Breakdown*
     - *Endgame & Conversion Vulnerabilities*
     - *Custom Training Plan & Actionable Exercises*

5. **Fast, Lightweight Web Dashboard:**
   - Serves on port `5000` with minimal memory consumption (~30MB RAM).
   - Dynamic overview page with key stats (total games, win/loss/draw record, win rate, blunder rate).
   - Visual bar chart of blunder trends and mistake velocity.
   - Interactive table of recent matches analyzed with direct links to games.
   - Chronological archive of historical reports (sortable oldest-to-newest or newest-to-oldest) rendering formatted Markdown reports.

6. **Hands-Off Automated Production Deployment:**
   - `install.sh` handles end-to-end setup with pre-flight checks, dependency installation, persistent directory permissions (no need to grant permissions repeatedly), systemd background service creation, UFW / OCI iptables firewall configuration, and 04:00 AM daily cron scheduling.

---

## Oracle Cloud Infrastructure (OCI) Firewall & Networking

If you are hosting this application on an **Oracle Cloud (OCI)** Ubuntu instance, please be aware that connection issues on port 5000 typically stem from two firewall layers:

### 1. OS-Level Firewall (iptables & UFW)
Standard OCI Ubuntu images include a default `iptables` rule with `REJECT` on all incoming ports except port 22. Even if UFW allows port 5000, iptables can block it.
To fix this instantly, `install.sh` automatically configures both UFW and iptables, or you can run:
```bash
sudo iptables -I INPUT 1 -p tcp --dport 5000 -m state --state NEW,ESTABLISHED -j ACCEPT
sudo netfilter-persistent save
# If UFW is enabled:
sudo ufw allow 5000/tcp
```

### 2. OCI Cloud Console (Security List / Network Security Group)
OCI blocks all incoming ports in the Virtual Cloud Network (VCN) by default. You must add an Ingress Rule in the OCI Console:
1. Navigate to: **Networking** &rarr; **Virtual Cloud Networks (VCN)** &rarr; Your VCN &rarr; **Security Lists** &rarr; **Default Security List**.
2. Click **Add Ingress Rules**:
   - **Stateless:** No (leave unchecked)
   - **Source CIDR:** `0.0.0.0/0`
   - **IP Protocol:** `TCP`
   - **Source Port Range:** All
   - **Destination Port Range:** `5000`
   - **Description:** `AI Chess Coach Web Dashboard`
3. Click **Add Ingress Rules**. The dashboard will immediately become reachable at `http://<YOUR_INSTANCE_PUBLIC_IP>:5000`.

---

## How to Obtain a Free Google Gemini API Key

The AI Coach uses Google's free-tier **Gemini 2.5 Flash** (`gemini-2.5-flash`) via Google AI Studio. Follow these quick steps to get your key:

1. **Visit Google AI Studio:**
   - Go to [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey) (or [https://aistudio.google.com/](https://aistudio.google.com/)).
   - Sign in with your standard Google Account.

2. **Generate the Key:**
   - Click the blue **"Create API Key"** (or **"Get API key"**) button.
   - Choose **"Create API key in new project"** (or select an existing Google Cloud project if you have one).
   - A modal will appear displaying your new API key string (e.g. `AIzaSy...`).

3. **Copy and Save:**
   - Click **Copy**. Note: Ensure you copy the entire key without extra quotes or spaces.
   - Free tier includes generous free requests per minute, which is more than enough for daily coaching telemetry analysis.

4. **Update / Configure on Your Server:**
   - Either re-run `sudo bash install.sh` (which will prompt you to enter/update your key).
   - Or directly edit the environment file:
     ```bash
     sudo nano /etc/chess-coach.env
     ```
     Set:
     ```bash
     GEMINI_API_KEY=AIzaSy...your_copied_key_here
     ```
     Save and restart the background dashboard service:
     ```bash
     sudo systemctl restart chess-coach
     ```

---

## Quick Start / Installation

### 1. Run the Production Installer
Run the installer as root (or with sudo):

```bash
sudo bash install.sh
```

During installation, the installer will:
1. Verify system prerequisites (OS, CPU, memory, internet).
2. Install system packages: `python3-pip`, `python3-venv`, `stockfish`, `git`, `cron`.
3. Set up directory `/data` and `/data/reports` with permissions for the non-root application user so that automatic runs never fail with permission errors.
4. Set up Python virtual environment (`venv/`) and install requirements.
5. Securely prompt for your `GEMINI_API_KEY` (hidden input) and store it in `/etc/chess-coach.env` with restricted `chmod 600` permissions.
6. Install and enable the `chess-coach.service` systemd service so the dashboard starts automatically on boot.
7. Configure a daily cron trigger at **04:00 AM server time** in `/etc/cron.d/chess-coach-daily`.

---

## Configuration & Environment Variables

The application reads configuration from `/etc/chess-coach.env` (or environment variables):

| Variable | Default | Description |
|---|---|---|
| `CHESS_USERNAME` | `Troy05x` | Target player username on Chess.com & Lichess |
| `GEMINI_API_KEY` | *(prompted during install)* | Google AI Studio Gemini API key |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model name (supports gemini-3.8-flash, gemini-2.5-flash) |
| `DATA_DIR` | `/data` | Path for SQLite database and reports |
| `STOCKFISH_PATH` | `/usr/games/stockfish` | Path to Stockfish binary |
| `PORT` | `5000` | Web server listening port |

---

## Service Management & CLI Operations

### Check Web Server Status
```bash
sudo systemctl status chess-coach
```

### Restart Web Server
```bash
sudo systemctl restart chess-coach
```

### View Web Server Logs
```bash
sudo journalctl -u chess-coach -f
```

### Run Analysis Manually (On Demand)
You can manually run `engine.py` at any time to analyze yesterday's games:

```bash
sudo -u $(whoami) /usr/bin/env bash -c 'set -a; [ -f /etc/chess-coach.env ] && . /etc/chess-coach.env; set +a; ./venv/bin/python3 engine.py'
```

### Check Daily Cron Execution Logs
```bash
cat /data/cron_engine.log
```

---

## Database & Storage Layout

- `/data/chess_coach.db`: Lightweight SQLite database containing:
  - `daily_summaries`: Daily aggregate game counts, wins, losses, draws, blunders, average centipawn loss, report paths.
  - `games`: Individual game records, opponents, ratings, results, and blunder counts.
  - `mistake_events`: Detailed telemetry of each move where Troy05x incurred centipawn loss > 100, including move notation, best move, board FEN, game phase, and piece moved.
- `/data/reports/YYYY-MM-DD.md`: Historical daily markdown reports.

---

## Memory Footprint & Resource Bounds

Engine and Web Dashboard are strictly tuned for 1GB RAM instances:
- Stockfish Hash memory capped at **16 MB**.
- Stockfish Threads capped at **1**.
- Move evaluation timeout capped at **0.03 seconds**.
- Systemd service has `MemoryMax=250M` enforced to guarantee overall system stability.
- Web application consumes ~30MB memory under idle/normal load.
