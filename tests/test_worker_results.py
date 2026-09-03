import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import worker


class WorkerResultTests(unittest.TestCase):
    def test_scheduled_queue_dispatch_contains_only_run_id(self):
        run_id = "46f83017-9d38-4b02-a0bc-fd79864e0675"
        payload = json.dumps({"run_id": run_id})
        fake_redis = MagicMock()
        fake_redis.zrangebyscore.return_value = [payload]
        fake_redis.zrem.return_value = 1
        fake_redis.set.return_value = True
        with (
            patch.object(worker, "r", fake_redis),
            patch.object(
                worker,
                "cycle_run_dispatch_status",
                return_value=("private@example.com", "cycle-id"),
            ),
            patch.object(worker, "update_task") as update_task,
        ):
            self.assertEqual(worker.move_due_scheduled_tasks(), 1)

        queued_payload = fake_redis.rpush.call_args.args[1]
        self.assertEqual(json.loads(queued_payload), {"run_id": run_id})
        self.assertNotIn("private@example.com", queued_payload)
        self.assertNotIn("password", queued_payload.lower())
        update_task.assert_called_once_with(worker.DB_PATH, run_id, state="queued")

    def test_captcha_retry_does_not_restart_warp(self):
        task = {
            "run_id": "46f83017-9d38-4b02-a0bc-fd79864e0675",
            "email": "private@example.com",
            "retry_data": {},
        }
        fake_redis = MagicMock()
        with (
            patch.object(worker, "r", fake_redis),
            patch.object(worker, "set_task_feedback"),
            patch.object(worker, "restart_warp_for_retry") as restart,
            patch.object(worker.time, "time", return_value=1000),
        ):
            self.assertTrue(worker.schedule_failure_retry(task, "captcha_unsolved", 4))

        restart.assert_not_called()
        self.assertEqual(task["retry_data"]["warp_index"], 4)

    def test_page_timeout_is_one_delayed_retry_without_warp_restart(self):
        task = {
            "run_id": "46f83017-9d38-4b02-a0bc-fd79864e0675",
            "email": "private@example.com",
            "retry_data": {},
        }
        fake_redis = MagicMock()
        with (
            patch.object(worker, "r", fake_redis),
            patch.object(worker, "set_task_feedback"),
            patch.object(worker, "restart_warp_for_retry") as restart,
            patch.object(worker.time, "time", return_value=1000),
            patch.object(worker, "PAGE_TIMEOUT_RETRY_DELAY_SECONDS", 600),
        ):
            self.assertTrue(worker.schedule_failure_retry(task, "page_timeout", 4))

        restart.assert_not_called()
        self.assertEqual(task["retry_data"]["warp_index"], 4)
        self.assertEqual(fake_redis.zadd.call_args.args[0], worker.RETRY_QUEUE)
        retry_count = task["retry_data"]["page_timeout"]
        self.assertEqual(retry_count, 1)

    def test_captcha_invalid_and_login_page_timeout_use_short_retry_delays(self):
        """这两类失败必须短延迟排期。

        retry_delay 期间 retry_pending 存在，主循环 finally 就不删 task_lock，
        用户会看到「该账号有任务正在执行中」而无法重新提交。现有 captcha 类是
        7200s（锁 2 小时），对这两类可自愈失败绝不能照抄。
        """
        for error_type, expected_delay in (
            ("captcha_invalid", 120),
            ("login_page_timeout", 180),
        ):
            with self.subTest(error_type=error_type):
                task = {
                    "run_id": "46f83017-9d38-4b02-a0bc-fd79864e0675",
                    "email": "private@example.com",
                    "retry_data": {},
                }
                fake_redis = MagicMock()
                with (
                    patch.object(worker, "r", fake_redis),
                    patch.object(worker, "set_task_feedback"),
                    patch.object(worker, "restart_warp_for_retry") as restart,
                    patch.object(worker.time, "time", return_value=1000),
                ):
                    self.assertTrue(
                        worker.schedule_failure_retry(task, error_type, 4)
                    )

                restart.assert_not_called()
                self.assertEqual(task["retry_data"][error_type], 1)
                self.assertEqual(fake_redis.zadd.call_args.args[0], worker.RETRY_QUEUE)
                # run_at 必须落在 now + 预期短延迟，且远小于 captcha 类的 7200s
                run_at = list(fake_redis.zadd.call_args.args[1].values())[0]
                self.assertEqual(run_at, 1000 + expected_delay)
                self.assertLess(expected_delay, 600)

    def test_new_retryable_error_types_are_wired_into_dispatch_set(self):
        """policies 有条目还不够 —— 调度点的集合里也必须有，否则永不重试。

        线上真实踩过：policies 加了 captcha_invalid，但 run_task 的调度集合是
        硬编码的，schedule_failure_retry 从未被调用，retry_queue 始终为 0。
        """
        source = Path(worker.__file__).read_text(encoding="utf-8")
        dispatch_block = source.split('if final_error_type in {\n            "provider_timeout",')[1]
        dispatch_block = dispatch_block.split("}", 1)[0]
        for error_type in ("captcha_invalid", "login_page_timeout"):
            with self.subTest(error_type=error_type):
                self.assertIn(error_type, dispatch_block)
                self.assertIn(error_type, worker.ERROR_TYPE_MESSAGES)

    def test_log_redaction_removes_credentials_and_tokens(self):
        email = "user@example.com"
        password = "plain-password"
        token = "A" * 100
        output = worker.redact_log_line(
            f"email={email} password={password} Authorization=Bearer-{token}", email, password
        )
        self.assertNotIn(email, output)
        self.assertNotIn(password, output)
        self.assertNotIn(token, output)

    def test_parse_game_result(self):
        payload = {"title": "Game A", "status": "claimed"}
        line = f"prefix GAME_RESULT:{json.dumps(payload)}"
        self.assertEqual(worker.parse_game_result_line(line), ("Game A", "claimed"))

    def test_parse_game_result_accepts_deferred_and_unconfirmed(self):
        for status in ("deferred", "unconfirmed"):
            line = f'GAME_RESULT:{{"title":"Game A","status":"{status}"}}'
            self.assertEqual(worker.parse_game_result_line(line), ("Game A", status))

    def test_parse_game_result_rejects_unknown_status(self):
        line = 'GAME_RESULT:{"title":"Game A","status":"maybe"}'
        with self.assertRaises(ValueError):
            worker.parse_game_result_line(line)

    def test_network_retry_rotates_warp_index(self):
        with patch.object(worker, "WARP_PROXY_COUNT", 10):
            self.assertEqual(worker.next_retry_warp_index("network_timeout", 4), 5)
            self.assertEqual(worker.next_retry_warp_index("driver_crash", 9), 0)
            self.assertEqual(worker.next_retry_warp_index("provider_timeout", 4), 4)

    def test_task_warp_index_uses_retry_override(self):
        task = {"email": "user@example.com", "retry_data": {"warp_index": 15}}
        with (
            patch.object(worker, "WARP_PROXY_COUNT", 10),
            patch.object(worker, "get_warp_index_for_email", return_value=4),
        ):
            self.assertEqual(worker.get_task_warp_index(task), 5)
            self.assertEqual(worker.get_task_warp_index({"email": task["email"]}), 4)

    def test_summarize_multiple_games_and_partial_failure(self):
        successful, claimed, failed = worker.summarize_game_results(
            {
                "Game A": "claimed",
                "Game B": "owned",
                "Game C": "failed",
                "Game D": "deferred",
                "Game E": "unconfirmed",
            }
        )
        self.assertEqual(successful, ["Game A", "Game B"])
        self.assertEqual(claimed, ["Game A"])
        self.assertEqual(failed, ["Game C", "Game D", "Game E"])

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
        # 僵尸不算存活。进程被杀掉后如果父进程已经先退出，它会被重新父化给
        # PID 1；而在测试容器里 PID 1 就是跑 unittest 的这个进程本身，不会去
        # 回收它，于是它以 Z 状态留着 —— 而 os.kill(pid, 0) 对僵尸并不报错。
        # 此前这里把僵尸判成"仍然存活"，导致
        # test_terminate_process_group_kills_child_process 恒失败，
        # 而 terminate_process_group 本身是正确的（生产环境有
        # reap_child_processes 和容器的 init:true 负责回收）。
        return WorkerResultTests._process_stat(pid) != "Z"

    @staticmethod
    def _process_stat(pid):
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
                return f.read().split()[2]
        except OSError:
            return None


if __name__ == "__main__":
    unittest.main()
