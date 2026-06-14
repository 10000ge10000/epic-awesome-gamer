import json
import subprocess
import sys
import unittest

import worker


class WorkerResultTests(unittest.TestCase):
    def test_parse_game_result(self):
        payload = {"title": "Game A", "status": "claimed"}
        line = f"prefix GAME_RESULT:{json.dumps(payload)}"
        self.assertEqual(worker.parse_game_result_line(line), ("Game A", "claimed"))

    def test_parse_game_result_rejects_unknown_status(self):
        line = 'GAME_RESULT:{"title":"Game A","status":"maybe"}'
        with self.assertRaises(ValueError):
            worker.parse_game_result_line(line)

    def test_summarize_multiple_games_and_partial_failure(self):
        successful, claimed, failed = worker.summarize_game_results(
            {
                "Game A": "claimed",
                "Game B": "owned",
                "Game C": "failed",
            }
        )
        self.assertEqual(successful, ["Game A", "Game B"])
        self.assertEqual(claimed, ["Game A"])
        self.assertEqual(failed, ["Game C"])

    def test_process_output_hard_timeout(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            list(worker.iter_process_output(process, 1))
        process.wait(timeout=2)
        process.stdout.close()
        self.assertIsNotNone(process.returncode)


if __name__ == "__main__":
    unittest.main()
