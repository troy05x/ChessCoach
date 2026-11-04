#!/usr/bin/env python3
"""
engine.py - Background Core Analyzer for AI Chess Coach

Features:
- Calculates yesterday's date automatically (UTC).
- Fetches yesterday's games for user 'Troy05x' from Chess.com and Lichess.
- Analyzes games with local Stockfish binary under strict resource constraints:
  (1 thread, 16MB hash memory, 0.03s max evaluation time per move).
- Extracts moves where user centipawn loss > 100 (mistakes) or > 200 (blunders).
- Records phase of game (Opening/Middlegame/Endgame), piece moved, opponent, result.
- Packages daily telemetry and historical context into Gemini 2.5 Flash prompt via `google-genai`.
- Generates structured Markdown report saved to /data/reports/YYYY-MM-DD.md.
- Updates SQLite database /data/chess_coach.db with game summaries, blunder records,
  and provides historical rolling context (7-day and 30-day patterns).
"""

import os
import sys
import json
import sqlite3
import datetime
import logging
import io
import shutil
from typing import List, Dict, Any, Optional, Tuple

import requests
import chess
import chess.pgn
import chess.engine

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("chess_coach_engine")

# Configuration paths & environment variables
# Supports DATA_DIR environment variable, defaulting to /data or ./data if /data is not writable
DEFAULT_DATA_DIR = os.environ.get("DATA_DIR", "/data")
if not os.path.exists(DEFAULT_DATA_DIR):
    try:
        os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)
    except PermissionError:
        DEFAULT_DATA_DIR = os.path.abspath("./data")
        os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)

REPORTS_DIR = os.path.join(DEFAULT_DATA_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)
DB_PATH = os.path.join(DEFAULT_DATA_DIR, "chess_coach.db")
STATUS_FILE = os.path.join(DEFAULT_DATA_DIR, "engine_status.json")

TARGET_USERNAME = os.environ.get("CHESS_USERNAME", "Troy05x")


def update_status(stage: str, message: str, percent: int = 0, extra: Optional[Dict[str, Any]] = None) -> None:
    """Updates background analyzer status file for the web dashboard."""
    status_data = {
        "stage": stage,
        "message": message,
        "percent": percent,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "extra": extra or {}
    }
    try:
        tmp_file = f"{STATUS_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=2)
        os.replace(tmp_file, STATUS_FILE)
    except Exception as e:
        logger.warning(f"Could not update status file: {e}")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Candidate paths for stockfish executable
STOCKFISH_CANDIDATES = [
    os.environ.get("STOCKFISH_PATH", ""),
    shutil.which("stockfish") or "",
    "/usr/games/stockfish",
    "/usr/bin/stockfish",
    "/usr/local/bin/stockfish",
]
STOCKFISH_PATH = next((p for p in STOCKFISH_CANDIDATES if p and os.path.isfile(p)), "stockfish")

HTTP_HEADERS = {
    "User-Agent": f"AIChessCoach/1.0 (Player: {TARGET_USERNAME}; Cloud-Instance-Analyzer)"
}


def init_db(db_path: str = DB_PATH) -> None:
    """Initializes SQLite database schema for daily aggregates, game telemetry, and recurring patterns."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        # Daily summary table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_summaries (
                date TEXT PRIMARY KEY,
                total_games INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                draws INTEGER NOT NULL,
                total_mistakes INTEGER NOT NULL,
                total_blunders INTEGER NOT NULL,
                avg_cp_loss REAL DEFAULT 0.0,
                report_path TEXT,
                generated_at TEXT NOT NULL
            );
        """)
        # Detailed game records table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                date TEXT NOT NULL,
                user_color TEXT NOT NULL,
                opponent TEXT NOT NULL,
                opponent_rating INTEGER,
                user_rating INTEGER,
                result TEXT NOT NULL,
                time_control TEXT,
                mistakes_count INTEGER NOT NULL,
                blunders_count INTEGER NOT NULL,
                opening TEXT,
                pgn_url TEXT
            );
        """)
        # Specific mistakes/blunders table for macro pattern discovery
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mistake_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT NOT NULL,
                date TEXT NOT NULL,
                move_number INTEGER NOT NULL,
                user_move TEXT NOT NULL,
                best_move TEXT,
                cp_loss INTEGER NOT NULL,
                is_blunder INTEGER NOT NULL,
                phase TEXT NOT NULL,
                piece_type TEXT NOT NULL,
                fen_before TEXT,
                FOREIGN KEY (game_id) REFERENCES games(game_id)
            );
        """)
        conn.commit()


def get_target_date() -> datetime.date:
    """Returns yesterday's date in UTC."""
    return datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)


def fetch_chess_com_games(username: str, target_date: datetime.date) -> List[Dict[str, Any]]:
    """
    Fetches games played by username on target_date from Chess.com public archives.
    Endpoint: https://api.chess.com/pub/player/{username}/games/{YYYY}/{MM}
    """
    year_str = target_date.strftime("%Y")
    month_str = target_date.strftime("%m")
    url = f"https://api.chess.com/pub/player/{username.lower()}/games/{year_str}/{month_str}"

    logger.info(f"Fetching Chess.com games from {url}...")
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=15)
        if resp.status_code == 404:
            logger.info("No archive found for this month on Chess.com.")
            return []
        resp.raise_for_status()
        data = resp.json()
        games = data.get("games", [])
    except Exception as e:
        logger.warning(f"Failed to fetch Chess.com games: {e}")
        return []

    target_date_str = target_date.strftime("%Y.%m.%d")
    matched_games = []
    for g in games:
        # Check end_time timestamp or PGN Date header
        end_time = g.get("end_time")
        if end_time:
            game_date = datetime.datetime.fromtimestamp(end_time, tz=datetime.timezone.utc).date()
            if game_date == target_date:
                matched_games.append({
                    "platform": "chess.com",
                    "url": g.get("url", ""),
                    "pgn": g.get("pgn", ""),
                    "raw": g
                })
        elif target_date_str in g.get("pgn", ""):
            matched_games.append({
                "platform": "chess.com",
                "url": g.get("url", ""),
                "pgn": g.get("pgn", ""),
                "raw": g
            })

    logger.info(f"Found {len(matched_games)} Chess.com game(s) for {target_date}.")
    return matched_games


def fetch_lichess_games(username: str, target_date: datetime.date) -> List[Dict[str, Any]]:
    """
    Fetches games played by username on target_date from Lichess public API.
    Endpoint: https://lichess.org/api/games/user/{username}
    Time boundaries in milliseconds (since UTC midnight to midnight).
    """
    dt_start = datetime.datetime.combine(target_date, datetime.time.min, tzinfo=datetime.timezone.utc)
    dt_end = datetime.datetime.combine(target_date, datetime.time.max, tzinfo=datetime.timezone.utc)
    since_ms = int(dt_start.timestamp() * 1000)
    until_ms = int(dt_end.timestamp() * 1000)

    url = f"https://lichess.org/api/games/user/{username}"
    params = {
        "since": since_ms,
        "until": until_ms,
        "pgnInJson": "false",
        "opening": "true",
        "clocks": "false",
        "evals": "false"
    }
    headers = dict(HTTP_HEADERS)
    headers["Accept"] = "application/x-ndjson"

    logger.info(f"Fetching Lichess games for {target_date}...")
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code == 404:
            logger.info("Lichess user not found or no games.")
            return []
        resp.raise_for_status()
        raw_text = resp.text.strip()
        if not raw_text:
            return []
    except Exception as e:
        logger.warning(f"Failed to fetch Lichess games: {e}")
        return []

    matched_games = []
    # Lichess returns ndjson stream if Accept: application/x-ndjson
    for line in raw_text.split("\n"):
        if not line.strip():
            continue
        try:
            game_data = json.loads(line)
            # Reconstruct PGN or extract game id
            game_id = game_data.get("id", "")
            pgn_url = f"https://lichess.org/{game_id}"

            # Fetch raw PGN for this specific game
            pgn_resp = requests.get(f"{pgn_url}.pgn", headers=HTTP_HEADERS, timeout=10)
            pgn_text = pgn_resp.text if pgn_resp.status_code == 200 else ""

            matched_games.append({
                "platform": "lichess",
                "url": pgn_url,
                "pgn": pgn_text,
                "raw": game_data
            })
        except Exception as e:
            logger.debug(f"Error parsing lichess ndjson line: {e}")
            continue

    logger.info(f"Found {len(matched_games)} Lichess game(s) for {target_date}.")
    return matched_games


def determine_phase(board: chess.Board) -> str:
    """
    Determines game phase (Opening, Middlegame, Endgame) based on move count and piece material.
    """
    total_pieces = len(board.piece_map())
    fullmove = board.fullmove_number
    # Queens on board
    queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK))

    if fullmove <= 10:
        return "Opening"
    elif total_pieces <= 10 or queens == 0:
        return "Endgame"
    else:
        return "Middlegame"


def analyze_game_with_stockfish(
    game_dict: Dict[str, Any],
    username: str,
    stockfish_path: str = STOCKFISH_PATH
) -> Optional[Dict[str, Any]]:
    """
    Parses game PGN and evaluates Troy05x's moves using Stockfish with strictly bounded resources:
    - Threads: 1
    - Hash: 16 MB
    - time_limit: 0.03 seconds per move
    Calculates centipawn loss specifically for Troy05x:
    - centipawn loss > 100: Mistake
    - centipawn loss > 200: Blunder
    """
    pgn_str = game_dict.get("pgn", "")
    if not pgn_str:
        return None

    pgn_io = io.StringIO(pgn_str)
    game = chess.pgn.read_game(pgn_io)
    if not game:
        return None

    headers = game.headers
    white_player = headers.get("White", "").strip()
    black_player = headers.get("Black", "").strip()

    user_is_white = username.lower() in white_player.lower()
    user_is_black = username.lower() in black_player.lower()

    if not user_is_white and not user_is_black:
        # Username not matched directly
        logger.debug(f"Skipping game: {white_player} vs {black_player} - username {username} not found")
        return None

    user_color = chess.WHITE if user_is_white else chess.BLACK
    opponent_name = black_player if user_is_white else white_player
    user_elo = headers.get("WhiteElo" if user_is_white else "BlackElo", "Unknown")
    opp_elo = headers.get("BlackElo" if user_is_white else "WhiteElo", "Unknown")
    result_code = headers.get("Result", "*")
    opening = headers.get("Opening", headers.get("ECO", "Unknown"))
    time_control = headers.get("TimeControl", "Standard")
    game_url = game_dict.get("url", "")
    # Robust unique ID: extract from URL, headers, or PGN hash
    url_id = game_url.rstrip("/").split("/")[-1] if game_url else ""
    header_site = headers.get("Site", "").rstrip("/").split("/")[-1]
    header_link = headers.get("Link", "").rstrip("/").split("/")[-1]
    game_id = url_id or (header_site if header_site and header_site != "Chess.com" else "") or header_link or f"game_{abs(hash(pgn_str)) % 100000000}"

    if user_is_white:
        if result_code == "1-0":
            user_result = "Win"
        elif result_code == "0-1":
            user_result = "Loss"
        else:
            user_result = "Draw"
    else:
        if result_code == "0-1":
            user_result = "Win"
        elif result_code == "1-0":
            user_result = "Loss"
        else:
            user_result = "Draw"

    # Initialize Stockfish with ultra-conservative resource limits for 1GB RAM instance
    mistakes = []
    blunders = []
    total_cp_loss = 0
    analyzed_moves_count = 0

    try:
        # Launch engine
        engine = chess.engine.SimpleEngine.popen_uci(stockfish_path)
        # Apply strict resource limits
        engine.configure({"Threads": 1, "Hash": 16})
        time_limit = chess.engine.Limit(time=0.03)

        board = game.board()
        for node in game.mainline():
            move = node.move
            is_user_turn = (board.turn == user_color)
            fen_before = board.fen()
            phase = determine_phase(board)
            piece = board.piece_at(move.from_square)
            piece_name = chess.piece_name(piece.piece_type).capitalize() if piece else "Piece"

            if is_user_turn:
                # 1. Evaluate position before Troy05x plays to determine best move and baseline eval
                info_before = engine.analyse(board, time_limit)
                score_before = info_before["score"].pov(user_color)
                best_move = info_before.get("pv", [None])[0]

                # 2. Make move on board
                board.push(move)

                # 3. Evaluate position after Troy05x played
                info_after = engine.analyse(board, time_limit)
                score_after = info_after["score"].pov(user_color)

                # Calculate Centipawn Loss (relative to Troy05x)
                # Convert mates to high score (+/- 10000 cp)
                cp_before = score_before.score(mate_score=10000)
                cp_after = score_after.score(mate_score=10000)

                if cp_before is not None and cp_after is not None:
                    cp_loss = max(0, cp_before - cp_after)
                    total_cp_loss += cp_loss
                    analyzed_moves_count += 1

                    move_san = node.san() if hasattr(node, "san") else str(move)
                    best_san = board.san(best_move) if best_move and best_move in board.legal_moves else str(best_move)

                    mistake_record = {
                        "move_number": board.fullmove_number,
                        "user_move": move_san,
                        "best_move": best_san,
                        "cp_loss": cp_loss,
                        "is_blunder": int(cp_loss > 200),
                        "phase": phase,
                        "piece_type": piece_name,
                        "fen_before": fen_before
                    }

                    if cp_loss > 200:
                        blunders.append(mistake_record)
                    elif cp_loss > 100:
                        mistakes.append(mistake_record)
            else:
                board.push(move)

        engine.quit()

    except Exception as e:
        logger.error(f"Stockfish analysis encountered error for game {game_id}: {e}")
        try:
            engine.quit()
        except Exception:
            pass

    return {
        "game_id": game_id,
        "platform": game_dict.get("platform", "unknown"),
        "user_color": "White" if user_is_white else "Black",
        "opponent": opponent_name,
        "user_rating": user_elo,
        "opponent_rating": opp_elo,
        "result": user_result,
        "opening": opening,
        "time_control": time_control,
        "url": game_url,
        "mistakes": mistakes,
        "blunders": blunders,
        "total_moves_analyzed": analyzed_moves_count,
        "avg_cp_loss": round(total_cp_loss / max(1, analyzed_moves_count), 2)
    }


def get_historical_pattern_stats(db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    Fetches past 7-day and 30-day pattern trends from SQLite database
    to provide the AI coach and dashboard with multi-day macro context.
    """
    stats = {
        "last_7_days": {"total_games": 0, "blunders": 0, "mistakes": 0, "frequent_blunder_phases": {}, "frequent_pieces": {}},
        "last_30_days": {"total_games": 0, "blunders": 0, "mistakes": 0}
    }
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # 7-day aggregates
            cursor.execute("""
                SELECT SUM(total_games), SUM(total_mistakes), SUM(total_blunders)
                FROM daily_summaries
                WHERE date >= date('now', '-7 days')
            """)
            row7 = cursor.fetchone()
            if row7 and row7[0] is not None:
                stats["last_7_days"]["total_games"] = row7[0] or 0
                stats["last_7_days"]["mistakes"] = row7[1] or 0
                stats["last_7_days"]["blunders"] = row7[2] or 0

            # 7-day recurring mistake phases & pieces
            cursor.execute("""
                SELECT phase, COUNT(*) as cnt
                FROM mistake_events
                WHERE date >= date('now', '-7 days')
                GROUP BY phase ORDER BY cnt DESC
            """)
            stats["last_7_days"]["frequent_blunder_phases"] = dict(cursor.fetchall())

            cursor.execute("""
                SELECT piece_type, COUNT(*) as cnt
                FROM mistake_events
                WHERE date >= date('now', '-7 days')
                GROUP BY piece_type ORDER BY cnt DESC LIMIT 5
            """)
            stats["last_7_days"]["frequent_pieces"] = dict(cursor.fetchall())

            # 30-day aggregates
            cursor.execute("""
                SELECT SUM(total_games), SUM(total_mistakes), SUM(total_blunders)
                FROM daily_summaries
                WHERE date >= date('now', '-30 days')
            """)
            row30 = cursor.fetchone()
            if row30 and row30[0] is not None:
                stats["last_30_days"]["total_games"] = row30[0] or 0
                stats["last_30_days"]["mistakes"] = row30[1] or 0
                stats["last_30_days"]["blunders"] = row30[2] or 0

    except Exception as e:
        logger.warning(f"Could not compute historical pattern stats: {e}")

    return stats


def generate_gemini_report(
    date_str: str,
    username: str,
    daily_stats: Dict[str, Any],
    game_analyses: List[Dict[str, Any]],
    historical_stats: Dict[str, Any],
    api_key: str = GEMINI_API_KEY
) -> str:
    """
    Sends structured chess telemetry and pattern data to Google Gemini 2.5 Flash
    to generate an actionable macro coaching report.
    Supports both official `google-genai` SDK and zero-dependency lightweight REST API
    (ensuring full compatibility across all Python versions, including Python 3.8 on Ubuntu 20.04).
    """
    if not api_key:
        logger.warning("No GEMINI_API_KEY provided. Generating fallback analytical report.")
        return generate_offline_report(date_str, username, daily_stats, game_analyses, historical_stats)

    # Build clean JSON payload for Gemini prompt
    payload = {
        "player": username,
        "report_date": date_str,
        "daily_summary": daily_stats,
        "historical_macro_trends": historical_stats,
        "games": [
            {
                "game_id": g["game_id"],
                "platform": g["platform"],
                "user_color": g["user_color"],
                "opponent": g["opponent"],
                "result": g["result"],
                "opening": g["opening"],
                "mistake_count": len(g["mistakes"]),
                "blunder_count": len(g["blunders"]),
                "key_mistakes": g["mistakes"][:3],
                "key_blunders": g["blunders"][:3]
            }
            for g in game_analyses
        ]
    }

    system_instruction = (
        "You are an Elite Grandmaster AI Chess Coach. Your role is not just to point out individual move blunders, "
        "but to identify systemic, psychological, and recurring macro patterns in the player's games. "
        "Examine recurring positional themes, game phases where the player loses focus, piece coordination flaws, "
        "and time-control habits. Connect yesterday's mistakes to the player's rolling weekly and monthly trends."
    )

    user_prompt = f"""
Analyze the following telemetry for chess player '{username}' for date {date_str}.

Telemetry Data:
```json
{json.dumps(payload, indent=2)}
```

Generate a deep, engaging, and highly structured daily coaching report in Markdown format.
You MUST include these exact main sections (using H2 ## headers):
1. ## Executive Performance Overview
   - Summary of games played, scoreline, win rate, and overall play quality.
2. ## Recurring Macro Patterns & Psychological Blindspots
   - Deep dive into WHY mistakes happen repeatedly (e.g. rushing in the middlegame, miscalculating pawn structures, moving pieces prematurely).
   - Compare yesterday's patterns against their 7-day and 30-day macro tendencies.
3. ## Strategic Blindspots
   - Positional weaknesses, pawn structure mistakes, square weaknesses, or piece coordination problems.
4. ## Tactical Failures & Blunder Breakdown
   - Tactical oversight themes (missed pins, fork vulnerabilities, back-rank laziness, hanging pieces).
5. ## Endgame & Conversion Vulnerabilities
   - How the player converted winning advantages or collapsed in equal/drawn endgames.
6. ## Custom Training Plan & Actionable Exercises
   - Concrete, personalized exercises for today (e.g., 'Study Rook Endgames with 3 pawns vs 2', 'Solve 15 Defensive Tactic Puzzles', 'Play 15+10 Rapid with deliberate 5-second check before pawn pushes').

Provide direct, actionable, coach-grade advice. Keep formatting sharp with bullet points and bold emphasis.
"""

    # Method 1: Try google-genai SDK if available in current Python environment
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_prompt,
            config={
                "system_instruction": system_instruction,
                "temperature": 0.3,
            }
        )
        if response and response.text:
            logger.info("Successfully generated coaching report via google-genai SDK.")
            return response.text
    except ImportError:
        logger.info("google-genai SDK not installed; using direct lightweight Gemini REST API.")
    except Exception as e:
        logger.warning(f"google-genai SDK query failed ({e}); attempting direct REST API fallback...")

    # Method 2: Direct HTTP REST call to Gemini 2.5 Flash API via requests
    # Fully compatible with Python 3.8+ on Ubuntu 20.04 with zero SDK dependencies
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        rest_body = {
            "system_instruction": {
                "parts": [{"text": system_instruction}]
            },
            "contents": [
                {
                    "parts": [{"text": user_prompt}]
                }
            ],
            "generationConfig": {
                "temperature": 0.3
            }
        }
        resp = requests.post(
            url,
            json=rest_body,
            headers={"Content-Type": "application/json"},
            timeout=45
        )
        if resp.status_code == 200:
            result = resp.json()
            candidates = result.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text_parts = [p.get("text", "") for p in parts if "text" in p]
                report_text = "".join(text_parts).strip()
                if report_text:
                    logger.info("Successfully generated coaching report via direct Gemini REST API.")
                    return report_text
        else:
            logger.error(f"Gemini REST API returned error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Direct Gemini REST API request failed: {e}")

    # Fallback to local offline template if API key is invalid or unreachable
    logger.info("Falling back to offline analytical report.")
    return generate_offline_report(date_str, username, daily_stats, game_analyses, historical_stats)


def generate_offline_report(
    date_str: str,
    username: str,
    daily_stats: Dict[str, Any],
    game_analyses: List[Dict[str, Any]],
    historical_stats: Dict[str, Any]
) -> str:
    """Fallback generator when Gemini API key is missing or offline."""
    total_games = daily_stats.get("total_games", 0)
    wins = daily_stats.get("wins", 0)
    losses = daily_stats.get("losses", 0)
    draws = daily_stats.get("draws", 0)
    blunders = daily_stats.get("blunders", 0)
    mistakes = daily_stats.get("mistakes", 0)

    report = f"""# Chess Coaching Report: {date_str}
**Player:** {username}
**Date:** {date_str}
**Generated:** {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

---

## Executive Performance Overview
- **Games Analyzed:** {total_games} (Wins: {wins}, Losses: {losses}, Draws: {draws})
- **Blunders (>200 cp):** {blunders}
- **Mistakes (>100 cp):** {mistakes}
- **Average Centipawn Loss:** {daily_stats.get('avg_cp_loss', 0.0)} cp

## Recurring Macro Patterns & Psychological Blindspots
Based on the Stockfish evaluation, the majority of errors were concentrated in:
"""
    # Phase distribution
    phase_counts = {}
    for g in game_analyses:
        for b in g.get("blunders", []) + g.get("mistakes", []):
            phase = b.get("phase", "Middlegame")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1

    for phase, count in phase_counts.items():
        report += f"- **{phase}:** {count} inaccuracies/blunders observed.\n"

    report += f"""
### Historical Pattern Context (Rolling Trends)
- **Past 7 Days:** {historical_stats.get('last_7_days', {}).get('total_games', 0)} games played, {historical_stats.get('last_7_days', {}).get('blunders', 0)} blunders recorded.
- **Past 30 Days:** {historical_stats.get('last_30_days', {}).get('total_games', 0)} games played, {historical_stats.get('last_30_days', {}).get('blunders', 0)} blunders recorded.

## Strategic Blindspots
"""
    if not game_analyses:
        report += "No strategic blindspots detected.\n"
    else:
        for g in game_analyses:
            report += f"- **Game vs {g['opponent']} ({g['platform']}):** Result: {g['result']} as {g['user_color']} ({g['opening']}).\n"

    report += """
## Tactical Failures & Blunder Breakdown
"""
    has_blunders = False
    for g in game_analyses:
        for b in g.get("blunders", []):
            has_blunders = True
            report += f"- Move {b['move_number']} ({b['phase']}): Played `{b['user_move']}` (lost {b['cp_loss']} cp). Best move was `{b['best_move']}`.\n"
    if not has_blunders:
        report += "No major blunders (>200 centipawns) detected today! Excellent tactical discipline.\n"

    report += """
## Endgame & Conversion Vulnerabilities
- Review transitions into rook and minor piece endgames where king activity is crucial.

## Custom Training Plan & Actionable Exercises
1. **Candidate Move Discipline:** Spend at least 10 seconds evaluating opponent threats before committing any pawn or knight move.
2. **Tactics Practice:** Solve 15 puzzles focused on pins and discovered checks.
3. **Endgame Focus:** Practice Lucena and Philidor positions against Stockfish.
"""
    return report


def generate_no_games_report(date_str: str, username: str) -> str:
    """Generates clean markdown report when no games were played yesterday."""
    return f"""# Daily Chess Report: {date_str}

**Player:** {username}
**Date:** {date_str}
**Status:** No games played

---

## Daily Summary
No games were played by **{username}** on Chess.com or Lichess on **{date_str}**.

### Daily Coaching Tip
Rest days and study days are essential for consolidating tactical and strategic knowledge.
- Review your previous master annotations or solve 10 puzzle problems on Lichess.
- Rest well and return with sharp mental focus for tomorrow's sessions.
"""


def save_report_and_db(
    target_date: datetime.date,
    daily_stats: Dict[str, Any],
    game_analyses: List[Dict[str, Any]],
    report_content: str,
    db_path: str = DB_PATH,
    reports_dir: str = REPORTS_DIR
) -> str:
    """Saves markdown report to disk and updates SQLite database with all telemetry."""
    date_str = target_date.strftime("%Y-%m-%d")
    report_filename = f"{date_str}.md"
    report_path = os.path.join(reports_dir, report_filename)

    # 1. Write markdown report
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)
    logger.info(f"Saved daily report to {report_path}")

    # 2. Update SQLite DB
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # Insert or replace daily summary
        cursor.execute("""
            INSERT OR REPLACE INTO daily_summaries (
                date, total_games, wins, losses, draws, total_mistakes, total_blunders, avg_cp_loss, report_path, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str,
            daily_stats.get("total_games", 0),
            daily_stats.get("wins", 0),
            daily_stats.get("losses", 0),
            daily_stats.get("draws", 0),
            daily_stats.get("mistakes", 0),
            daily_stats.get("blunders", 0),
            daily_stats.get("avg_cp_loss", 0.0),
            report_path,
            now_iso
        ))

        # Insert game analyses & mistake events
        for g in game_analyses:
            cursor.execute("""
                INSERT OR REPLACE INTO games (
                    game_id, platform, date, user_color, opponent, opponent_rating,
                    user_rating, result, time_control, mistakes_count, blunders_count,
                    opening, pgn_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                g["game_id"],
                g["platform"],
                date_str,
                g["user_color"],
                g["opponent"],
                int(g["opponent_rating"]) if str(g["opponent_rating"]).isdigit() else None,
                int(g["user_rating"]) if str(g["user_rating"]).isdigit() else None,
                g["result"],
                g.get("time_control", ""),
                len(g.get("mistakes", [])),
                len(g.get("blunders", [])),
                g.get("opening", ""),
                g.get("url", "")
            ))

            for m in g.get("mistakes", []) + g.get("blunders", []):
                cursor.execute("""
                    INSERT INTO mistake_events (
                        game_id, date, move_number, user_move, best_move,
                        cp_loss, is_blunder, phase, piece_type, fen_before
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    g["game_id"],
                    date_str,
                    m["move_number"],
                    m["user_move"],
                    m["best_move"],
                    m["cp_loss"],
                    m["is_blunder"],
                    m["phase"],
                    m["piece_type"],
                    m.get("fen_before", "")
                ))

        conn.commit()
    logger.info("Updated SQLite database successfully.")
    return report_path


def run_pipeline(target_date: Optional[datetime.date] = None) -> None:
    """Main execution flow for background chess analysis."""
    if target_date is None:
        target_date = get_target_date()

    date_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"Starting AI Chess Coach Analyzer for {TARGET_USERNAME} on {date_str}...")

    update_status("running", f"Starting analysis pipeline for date {date_str}...", 5)
    init_db()

    # 1. Fetch games from Chess.com and Lichess
    update_status("running", f"Fetching matches played on {date_str} from Chess.com & Lichess...", 15)
    chess_games = fetch_chess_com_games(TARGET_USERNAME, target_date)
    lichess_games = fetch_lichess_games(TARGET_USERNAME, target_date)
    all_raw_games = chess_games + lichess_games

    total_games_count = len(all_raw_games)
    logger.info(f"Total raw games discovered: {total_games_count}")

    if total_games_count == 0:
        logger.info(f"No games played yesterday ({date_str}). Creating 'No games' record.")
        update_status("running", f"No games detected for {TARGET_USERNAME} on {date_str}. Creating resting summary...", 80)
        no_games_report = generate_no_games_report(date_str, TARGET_USERNAME)
        daily_stats = {
            "total_games": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "mistakes": 0,
            "blunders": 0,
            "avg_cp_loss": 0.0
        }
        save_report_and_db(target_date, daily_stats, [], no_games_report)
        update_status("idle", f"Completed: No games played on {date_str}.", 100, {"total_games": 0, "date": date_str})
        logger.info("Pipeline completed successfully for zero-game day.")
        return

    # 2. Analyze games with Stockfish
    analyzed_games = []
    wins = 0
    losses = 0
    draws = 0
    total_mistakes = 0
    total_blunders = 0
    total_cp_losses = []

    for idx, raw_game in enumerate(all_raw_games, 1):
        pct = int(20 + ((idx / total_games_count) * 50))
        msg = f"Stockfish evaluating game {idx}/{total_games_count} ({raw_game.get('platform')})..."
        logger.info(msg)
        update_status("running", msg, pct)
        analysis = analyze_game_with_stockfish(raw_game, TARGET_USERNAME, STOCKFISH_PATH)
        if analysis:
            analyzed_games.append(analysis)
            res = analysis.get("result", "")
            if res == "Win":
                wins += 1
            elif res == "Loss":
                losses += 1
            else:
                draws += 1
            m_cnt = len(analysis.get("mistakes", []))
            b_cnt = len(analysis.get("blunders", []))
            total_mistakes += m_cnt
            total_blunders += b_cnt
            if analysis.get("avg_cp_loss"):
                total_cp_losses.append(analysis["avg_cp_loss"])

    avg_loss = round(sum(total_cp_losses) / max(1, len(total_cp_losses)), 2)
    daily_stats = {
        "total_games": len(analyzed_games),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "mistakes": total_mistakes,
        "blunders": total_blunders,
        "avg_cp_loss": avg_loss
    }

    # 3. Retrieve historical trends (7-day and 30-day patterns)
    update_status("running", "Computing 7-day and 30-day recurring blunder patterns...", 75)
    historical_stats = get_historical_pattern_stats()

    # 4. Generate Gemini coaching report
    update_status("running", "Synthesizing coaching report via Google Gemini 2.5 Flash...", 85)
    report_content = generate_gemini_report(
        date_str=date_str,
        username=TARGET_USERNAME,
        daily_stats=daily_stats,
        game_analyses=analyzed_games,
        historical_stats=historical_stats
    )

    # 5. Save report and update DB
    update_status("running", "Persisting telemetry to SQLite and saving Markdown report...", 95)
    save_report_and_db(
        target_date=target_date,
        daily_stats=daily_stats,
        game_analyses=analyzed_games,
        report_content=report_content
    )

    update_status("idle", f"Successfully completed analysis for {date_str} ({len(analyzed_games)} games analyzed).", 100, {
        "date": date_str,
        "total_games": len(analyzed_games),
        "wins": wins,
        "losses": losses,
        "blunders": total_blunders
    })
    logger.info(f"Pipeline completed successfully. Analyzed {len(analyzed_games)} games.")


if __name__ == "__main__":
    run_pipeline()
