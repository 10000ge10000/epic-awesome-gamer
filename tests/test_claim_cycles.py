import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.secure_store import (
    cancel_obsolete_scheduled_runs,
    create_cycle_assignments,
    cycle_run_dispatch_status,
    ensure_schema,
    mark_cycle_complete_if_ready,
    promotion_cycle_id,
    record_game_result,
    scheduled_cycle_runs,
    set_active_promotion_cycle,
    task_cycle_is_active,
)


GAMES = [
    {"id": "namespace-a", "title": "Game A"},
    {"id": "namespace-b", "title": "Game B"},
]


class ClaimCycleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "kiosk.db")
        ensure_schema(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            for index in range(12):
                conn.execute(
                    """
                    INSERT INTO accounts(email, credential_ciphertext, profile_id)
                    VALUES (?, 'ciphertext', ?)
                    """,
                    (f"account-{index:02d}@example.com", f"profile-{index:02d}"),
                )

    def tearDown(self):
        self.tempdir.cleanup()

    def _activate_and_schedule(self, games=None):
        games = games or GAMES
        cycle_id, _changed = set_active_promotion_cycle(self.db_path, games)
        runs = create_cycle_assignments(
            self.db_path,
            cycle_id,
            games,
            start_at=1000,
            batch_size=5,
            batch_interval_seconds=600,
        )
        return cycle_id, runs

    def test_cycle_id_is_order_independent(self):
        self.assertEqual(promotion_cycle_id(GAMES), promotion_cycle_id(list(reversed(GAMES))))

    def test_accounts_are_persisted_in_batches_of_five_without_credentials(self):
        cycle_id, runs = self._activate_and_schedule()
        self.assertEqual(len(runs), 12)
        self.assertEqual([run_at for _run_id, run_at in runs], [1000] * 5 + [1600] * 5 + [2200] * 2)
        self.assertEqual(scheduled_cycle_runs(self.db_path, cycle_id), runs)

        with sqlite3.connect(self.db_path) as conn:
            retry_data = [
                row[0]
                for row in conn.execute(
                    "SELECT retry_data FROM task_runs WHERE state='scheduled'"
                )
            ]
        self.assertEqual(len(retry_data), 12)
        self.assertTrue(all("@example.com" not in payload for payload in retry_data))
        self.assertTrue(all("password" not in payload.lower() for payload in retry_data))

    def test_two_games_must_both_be_successful_before_completion(self):
        cycle_id, runs = self._activate_and_schedule()
        run_id = runs[0][0]
        self.assertFalse(mark_cycle_complete_if_ready(self.db_path, run_id))
        record_game_result(self.db_path, run_id, "Game A", "claimed")
        self.assertFalse(mark_cycle_complete_if_ready(self.db_path, run_id))
        record_game_result(self.db_path, run_id, "Game B", "owned")
        self.assertTrue(mark_cycle_complete_if_ready(self.db_path, run_id))
        self.assertTrue(mark_cycle_complete_if_ready(self.db_path, run_id))

        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM claim_cycle_completions WHERE cycle_id=?", (cycle_id,)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_any_non_success_result_blocks_completion(self):
        _cycle_id, runs = self._activate_and_schedule()
        for run_id, status in zip(
            [item[0] for item in runs[:3]], ["failed", "deferred", "unconfirmed"]
        ):
            with self.subTest(status=status):
                record_game_result(self.db_path, run_id, "Game A", "claimed")
                record_game_result(self.db_path, run_id, "Game B", status)
                self.assertFalse(mark_cycle_complete_if_ready(self.db_path, run_id))

    def test_unchanged_cycle_does_not_create_duplicate_assignments(self):
        cycle_id, first = self._activate_and_schedule()
        repeated_id, changed = set_active_promotion_cycle(self.db_path, list(reversed(GAMES)))
        second = create_cycle_assignments(
            self.db_path,
            repeated_id,
            GAMES,
            start_at=5000,
            batch_size=5,
            batch_interval_seconds=600,
        )
        self.assertEqual(repeated_id, cycle_id)
        self.assertFalse(changed)
        self.assertEqual(len(first), 12)
        self.assertEqual(second, [])

    def test_new_cycle_schedules_all_accounts_and_obsoletes_old_schedule(self):
        old_cycle, old_runs = self._activate_and_schedule()
        new_games = [{"id": "namespace-c", "title": "Game C"}]
        new_cycle, changed = set_active_promotion_cycle(self.db_path, new_games)
        self.assertTrue(changed)
        obsolete = cancel_obsolete_scheduled_runs(self.db_path, new_cycle)
        new_runs = create_cycle_assignments(
            self.db_path,
            new_cycle,
            new_games,
            start_at=4000,
            batch_size=5,
            batch_interval_seconds=600,
        )
        self.assertNotEqual(old_cycle, new_cycle)
        self.assertEqual(set(obsolete), {run_id for run_id, _run_at in old_runs})
        self.assertEqual(len(new_runs), 12)
        with self.assertRaises(RuntimeError):
            cycle_run_dispatch_status(self.db_path, old_runs[0][0])
        self.assertFalse(
            task_cycle_is_active(
                self.db_path,
                {"cycle_id": old_cycle, "expected_games": GAMES},
            )
        )


if __name__ == "__main__":
    unittest.main()
