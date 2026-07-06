import json
import os
import signal
import subprocess
import sys
import time
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
