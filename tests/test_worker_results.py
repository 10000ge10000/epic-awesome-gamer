import json
import os
import signal
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_process_output_replaces_invalid_utf8(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'\\xff\\xfeinvalid\\n'); sys.stdout.flush()",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        lines = list(worker.iter_process_output(process, 2))
        process.wait(timeout=2)
        process.stdout.close()

        self.assertIn("invalid", "".join(lines))

    def test_terminate_process_group_kills_child_process(self):
        child_code = "import time; time.sleep(30)"
        parent_code = (
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", parent_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            child_pid = int(process.stdout.readline().strip())
            worker.terminate_process_group(process, grace_seconds=1)
            process.wait(timeout=2)

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not self._process_exists(child_pid):
                    break
                time.sleep(0.1)
            else:
                self.fail(f"child process {child_pid} still exists after group termination")
        finally:
            if process.poll() is None:
                process.kill()
            if process.stdout:
                process.stdout.close()

    def test_reap_child_processes_reaps_zombie_child(self):
        if not hasattr(os, "fork"):
            self.skipTest("fork is required to create a local zombie process")

        pid = os.fork()
        if pid == 0:
            os._exit(0)

        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                stat = self._process_stat(pid)
                if stat and "Z" in stat:
                    break
                time.sleep(0.05)

            self.assertGreaterEqual(worker.reap_child_processes(), 1)
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def test_terminate_process_group_ignores_exited_process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process.wait(timeout=2)
        worker.terminate_process_group(process)
        if process.stdout:
            process.stdout.close()

    def test_restart_warp_control_503_uses_bounded_container_fallback(self):
        response = SimpleNamespace(status_code=503, text="busy")
        completed = SimpleNamespace(returncode=0, stdout="epic-warp\\n", stderr="")

        with (
            patch.object(worker, "WARP_CONTROL_URL_TEMPLATE", "http://epic-warp:18080/restart/{idx}"),
            patch.object(worker, "WARP_CONTROL_RESTART_RETRIES", 3),
            patch.object(worker, "WARP_CONTROL_RESTART_BACKOFF_SECONDS", 1),
            patch.object(worker, "WARP_CONTAINER_FALLBACK_RESTARTS", 1),
            patch.object(worker, "request_warp_control", return_value=response) as control,
            patch.object(worker, "wait_for_warp_recovery", side_effect=[False, False, False, True]) as wait,
            patch.object(worker.subprocess, "run", return_value=completed) as run,
            patch.object(worker.time, "sleep"),
        ):
            self.assertTrue(worker.restart_warp_container(4))

        self.assertEqual(control.call_count, 3)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(wait.call_count, 4)

    def test_wait_for_warp_recovery_accepts_proxy_when_health_is_busy(self):
        with (
            patch.object(worker, "_control_health_reports_ready", return_value=(False, "health status=503")) as health,
            patch.object(worker, "check_warp_proxy", return_value=(True, "104.28.0.1")) as check,
        ):
            self.assertTrue(worker.wait_for_warp_recovery(4, timeout_seconds=1))

        self.assertEqual(health.call_count, 1)
        self.assertEqual(check.call_count, 1)

    def test_request_warp_control_ignores_environment_proxy(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}):
            with patch.object(worker.requests, "Session") as session_cls:
                session = session_cls.return_value.__enter__.return_value
                session.request.return_value = SimpleNamespace(status_code=200, text="ok")

                response = worker.request_warp_control("GET", "http://epic-warp:18080/health", timeout=3)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(session.trust_env)
        session.request.assert_called_once_with("GET", "http://epic-warp:18080/health", timeout=3)

    def test_ensure_warp_ready_runs_recovery_once_per_check_cycle(self):
        with (
            patch.dict(os.environ, {"HTTP_PROXY": "http://epic-warp:19000"}),
            patch.object(worker, "WARP_MAX_RETRIES", 4),
            patch.object(worker, "WARP_CONTROL_RESTART_BACKOFF_SECONDS", 1),
            patch.object(
                worker,
                "check_warp_proxy",
                side_effect=[(False, "down"), (False, "starting"), (True, "104.28.0.1")],
            ),
            patch.object(worker, "restart_warp_container", return_value=False) as restart,
            patch.object(worker.time, "sleep"),
        ):
            self.assertTrue(worker.ensure_warp_ready(4))

        self.assertEqual(restart.call_count, 1)


    @staticmethod
    def _process_exists(pid):
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in result.stdout

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    @staticmethod
    def _process_stat(pid):
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
                return f.read().split()[2]
        except OSError:
            return None


if __name__ == "__main__":
    unittest.main()
