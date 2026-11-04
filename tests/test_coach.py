import unittest
import os
import tempfile
import sqlite3
import datetime
import chess

from engine import (
    init_db,
    determine_phase,
    analyze_game_with_stockfish,
    generate_no_games_report,
    save_report_and_db,
    get_historical_pattern_stats,
    STOCKFISH_PATH
)
import app


class TestEngineAndApp(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_coach.db")
        self.reports_dir = os.path.join(self.test_dir, "reports")
        os.makedirs(self.reports_dir, exist_ok=True)
        init_db(self.db_path)

    def test_determine_phase(self):
        board = chess.Board()
        self.assertEqual(determine_phase(board), "Opening")

    def test_no_games_report_generation(self):
        date_str = "2026-11-03"
        report = generate_no_games_report(date_str, "Troy05x")
        self.assertIn("No games played", report)
        self.assertIn("Troy05x", report)

    def test_stockfish_analysis_bounded(self):
        # Sample minimal game PGN where Troy05x plays White
        sample_pgn = """[Event "Live Chess"]
[Site "https://www.chess.com/game/live/1234567"]
[Date "2026.11.03"]
[White "Troy05x"]
[Black "Opponent"]
[Result "1-0"]
[WhiteElo "1200"]
[BlackElo "1200"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. O-O Nf6 5. d3 d6 1-0
"""
        game_dict = {
            "platform": "chess.com",
            "url": "https://www.chess.com/game/live/1234567",
            "pgn": sample_pgn
        }

        result = analyze_game_with_stockfish(game_dict, "Troy05x", STOCKFISH_PATH)
        self.assertIsNotNone(result)
        self.assertEqual(result["user_color"], "White")
        self.assertEqual(result["opponent"], "Opponent")
        self.assertEqual(result["result"], "Win")
        self.assertIsInstance(result["mistakes"], list)
        self.assertIsInstance(result["blunders"], list)

    def test_save_and_historical_stats(self):
        target_date = datetime.date(2026, 11, 3)
        daily_stats = {
            "total_games": 2,
            "wins": 1,
            "losses": 1,
            "draws": 0,
            "mistakes": 3,
            "blunders": 1,
            "avg_cp_loss": 85.5
        }
        game_analyses = [
            {
                "game_id": "test_g1",
                "platform": "chess.com",
                "user_color": "White",
                "opponent": "Opp1",
                "opponent_rating": 1250,
                "user_rating": 1300,
                "result": "Win",
                "time_control": "600",
                "opening": "Italian",
                "url": "https://chess.com/1",
                "mistakes": [{
                    "move_number": 12,
                    "user_move": "Nd4",
                    "best_move": "Nf3",
                    "cp_loss": 120,
                    "is_blunder": 0,
                    "phase": "Middlegame",
                    "piece_type": "Knight",
                    "fen_before": ""
                }],
                "blunders": [{
                    "move_number": 20,
                    "user_move": "Qe2",
                    "best_move": "Qd1",
                    "cp_loss": 250,
                    "is_blunder": 1,
                    "phase": "Middlegame",
                    "piece_type": "Queen",
                    "fen_before": ""
                }]
            }
        ]

        report_path = save_report_and_db(
            target_date=target_date,
            daily_stats=daily_stats,
            game_analyses=game_analyses,
            report_content="# Sample Report",
            db_path=self.db_path,
            reports_dir=self.reports_dir
        )
        self.assertTrue(os.path.exists(report_path))

        # Check DB
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            summary = cursor.execute("SELECT * FROM daily_summaries WHERE date='2026-11-03'").fetchone()
            self.assertIsNotNone(summary)
            self.assertEqual(summary[1], 2)  # total_games

            mistakes = cursor.execute("SELECT count(*) FROM mistake_events").fetchone()[0]
            self.assertEqual(mistakes, 2)

        # Check historical pattern calculation
        hist_stats = get_historical_pattern_stats(self.db_path)
        self.assertIn("last_7_days", hist_stats)
        self.assertIn("last_30_days", hist_stats)

    def test_flask_endpoints(self):
        client = app.app.test_client()
        r = client.get("/")
        self.assertEqual(r.status_code, 200)

        r_reports = client.get("/reports")
        self.assertEqual(r_reports.status_code, 200)

        r_api = client.get("/api/stats")
        self.assertEqual(r_api.status_code, 200)
        self.assertEqual(r_api.get_json()["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
