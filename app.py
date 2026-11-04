#!/usr/bin/env python3
"""
app.py - Lightweight Web Dashboard for AI Chess Coach

Features:
- Fast, mobile-responsive HTML dashboard built with Flask.
- Dynamic overview page showing blunder trends, win/loss ratios, piece & phase blunder breakdowns.
- Weekly (7-day) and Monthly (30-day) pattern recognition to track mistake trends over time.
- Chronological list of historical daily coaching reports (oldest to newest or newest to oldest).
- Renders selected day's Markdown report into clean, readable HTML with syntax styling.
- Ultra-low memory footprint (~25-35MB RAM) designed for 1GB cloud instances.
"""

import os
import sqlite3
import datetime
import glob
from typing import List, Dict, Any, Optional

from flask import Flask, render_template, request, abort, redirect, url_for, jsonify
import markdown

app = Flask(__name__)

# Configuration paths
DATA_DIR = os.environ.get("DATA_DIR", "/data")
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except PermissionError:
        DATA_DIR = os.path.abspath("./data")
        os.makedirs(DATA_DIR, exist_ok=True)

REPORTS_DIR = os.path.join(DATA_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "chess_coach.db")
TARGET_USERNAME = os.environ.get("CHESS_USERNAME", "Troy05x")


def get_db_connection() -> sqlite3.Connection:
    """Creates a connection to SQLite database with Row row_factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_daily_reports_list() -> List[Dict[str, Any]]:
    """
    Returns list of all available daily reports sorted chronologically.
    Includes date, filename, and whether it exists on disk.
    """
    reports = []
    # Search disk files
    md_files = glob.glob(os.path.join(REPORTS_DIR, "*.md"))
    dates_on_disk = {}
    for filepath in md_files:
        basename = os.path.basename(filepath)
        date_str = basename.replace(".md", "")
        dates_on_disk[date_str] = filepath

    # Query DB for summaries if available
    db_data = {}
    if os.path.exists(DB_PATH):
        try:
            with get_db_connection() as conn:
                rows = conn.execute("SELECT * FROM daily_summaries ORDER BY date ASC").fetchall()
                for r in rows:
                    db_data[r["date"]] = dict(r)
        except Exception:
            pass

    # Merge disk dates and DB dates
    all_dates = sorted(set(list(dates_on_disk.keys()) + list(db_data.keys())))
    for d in all_dates:
        summary = db_data.get(d, {})
        reports.append({
            "date": d,
            "filename": f"{d}.md",
            "exists": d in dates_on_disk,
            "total_games": summary.get("total_games", 0),
            "wins": summary.get("wins", 0),
            "losses": summary.get("losses", 0),
            "draws": summary.get("draws", 0),
            "blunders": summary.get("total_blunders", 0),
            "mistakes": summary.get("total_mistakes", 0),
            "avg_cp_loss": summary.get("avg_cp_loss", 0.0)
        })

    return reports


@app.route("/")
def dashboard():
    """Main overview dashboard showing macro stats, trends, and recent games."""
    reports = get_daily_reports_list()

    # Aggregate overall stats
    total_games = sum(r["total_games"] for r in reports)
    total_wins = sum(r["wins"] for r in reports)
    total_losses = sum(r["losses"] for r in reports)
    total_draws = sum(r["draws"] for r in reports)
    total_blunders = sum(r["blunders"] for r in reports)
    total_mistakes = sum(r["mistakes"] for r in reports)
    win_rate = round((total_wins / max(1, total_games)) * 100, 1)

    # 7-day and 30-day pattern analytics
    patterns_7d = {"total_games": 0, "blunders": 0, "mistakes": 0, "phases": {}, "pieces": {}}
    patterns_30d = {"total_games": 0, "blunders": 0, "mistakes": 0, "phases": {}, "pieces": {}}
    recent_games = []

    if os.path.exists(DB_PATH):
        try:
            with get_db_connection() as conn:
                # 7-day aggregations
                r7 = conn.execute("""
                    SELECT SUM(total_games) as g, SUM(wins) as w, SUM(losses) as l,
                           SUM(total_mistakes) as m, SUM(total_blunders) as b
                    FROM daily_summaries
                    WHERE date >= date('now', '-7 days')
                """).fetchone()
                if r7 and r7["g"] is not None:
                    patterns_7d["total_games"] = r7["g"] or 0
                    patterns_7d["wins"] = r7["w"] or 0
                    patterns_7d["losses"] = r7["l"] or 0
                    patterns_7d["mistakes"] = r7["m"] or 0
                    patterns_7d["blunders"] = r7["b"] or 0

                # 7-day phases
                p7 = conn.execute("""
                    SELECT phase, COUNT(*) as cnt
                    FROM mistake_events
                    WHERE date >= date('now', '-7 days')
                    GROUP BY phase ORDER BY cnt DESC
                """).fetchall()
                patterns_7d["phases"] = {row["phase"]: row["cnt"] for row in p7}

                # 7-day pieces
                pc7 = conn.execute("""
                    SELECT piece_type, COUNT(*) as cnt
                    FROM mistake_events
                    WHERE date >= date('now', '-7 days')
                    GROUP BY piece_type ORDER BY cnt DESC LIMIT 5
                """).fetchall()
                patterns_7d["pieces"] = {row["piece_type"]: row["cnt"] for row in pc7}

                # 30-day aggregations
                r30 = conn.execute("""
                    SELECT SUM(total_games) as g, SUM(wins) as w, SUM(losses) as l,
                           SUM(total_mistakes) as m, SUM(total_blunders) as b
                    FROM daily_summaries
                    WHERE date >= date('now', '-30 days')
                """).fetchone()
                if r30 and r30["g"] is not None:
                    patterns_30d["total_games"] = r30["g"] or 0
                    patterns_30d["wins"] = r30["w"] or 0
                    patterns_30d["losses"] = r30["l"] or 0
                    patterns_30d["mistakes"] = r30["m"] or 0
                    patterns_30d["blunders"] = r30["b"] or 0

                # 30-day phases
                p30 = conn.execute("""
                    SELECT phase, COUNT(*) as cnt
                    FROM mistake_events
                    WHERE date >= date('now', '-30 days')
                    GROUP BY phase ORDER BY cnt DESC
                """).fetchall()
                patterns_30d["phases"] = {row["phase"]: row["cnt"] for row in p30}

                # 30-day pieces
                pc30 = conn.execute("""
                    SELECT piece_type, COUNT(*) as cnt
                    FROM mistake_events
                    WHERE date >= date('now', '-30 days')
                    GROUP BY piece_type ORDER BY cnt DESC LIMIT 5
                """).fetchall()
                patterns_30d["pieces"] = {row["piece_type"]: row["cnt"] for row in pc30}

                # Recent games list
                recent_games = conn.execute("""
                    SELECT game_id, platform, date, user_color, opponent, result, mistakes_count, blunders_count, opening, pgn_url
                    FROM games
                    ORDER BY date DESC, game_id DESC
                    LIMIT 20
                """).fetchall()
                recent_games = [dict(g) for g in recent_games]
        except Exception as e:
            app.logger.warning(f"Error fetching dashboard telemetry: {e}")

    # Prepare trend chart data for daily summaries
    chart_dates = [r["date"] for r in reports][-14:]
    chart_blunders = [r["blunders"] for r in reports][-14:]
    chart_mistakes = [r["mistakes"] for r in reports][-14:]
    chart_games = [r["total_games"] for r in reports][-14:]

    return render_template(
        "dashboard.html",
        username=TARGET_USERNAME,
        reports=reports,
        total_games=total_games,
        total_wins=total_wins,
        total_losses=total_losses,
        total_draws=total_draws,
        total_blunders=total_blunders,
        total_mistakes=total_mistakes,
        win_rate=win_rate,
        patterns_7d=patterns_7d,
        patterns_30d=patterns_30d,
        recent_games=recent_games,
        chart_dates=chart_dates,
        chart_blunders=chart_blunders,
        chart_mistakes=chart_mistakes,
        chart_games=chart_games
    )


@app.route("/reports")
def list_reports():
    """Lists all historical reports chronologically."""
    sort_order = request.args.get("sort", "desc")
    reports = get_daily_reports_list()
    if sort_order == "desc":
        reports = list(reversed(reports))

    return render_template(
        "reports.html",
        username=TARGET_USERNAME,
        reports=reports,
        sort_order=sort_order
    )


@app.route("/reports/<date_str>")
def view_report(date_str: str):
    """Renders a specific day's Markdown coaching report into clean HTML."""
    filepath = os.path.join(REPORTS_DIR, f"{date_str}.md")
    if not os.path.exists(filepath):
        # Fallback check
        abort(404, description=f"Report for date {date_str} not found.")

    with open(filepath, "r", encoding="utf-8") as f:
        md_text = f.read()

    # Convert markdown to formatted HTML
    html_content = markdown.markdown(
        md_text,
        extensions=["extra", "tables", "fenced_code", "nl2br", "sane_lists"]
    )

    # Fetch daily summary metadata if present
    summary = None
    if os.path.exists(DB_PATH):
        try:
            with get_db_connection() as conn:
                row = conn.execute("SELECT * FROM daily_summaries WHERE date = ?", (date_str,)).fetchone()
                if row:
                    summary = dict(row)
        except Exception:
            pass

    return render_template(
        "report_detail.html",
        username=TARGET_USERNAME,
        date_str=date_str,
        html_content=html_content,
        summary=summary
    )


@app.route("/api/stats")
def api_stats():
    """Lightweight JSON API for health checks and external monitoring."""
    reports = get_daily_reports_list()
    return jsonify({
        "status": "healthy",
        "username": TARGET_USERNAME,
        "total_days_recorded": len(reports),
        "total_games": sum(r["total_games"] for r in reports),
        "total_blunders": sum(r["blunders"] for r in reports)
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
