import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import codeserver_inner as inner


class SidecarHandoffTests(unittest.TestCase):
    def test_waits_until_old_job_leaves_queue(self):
        with mock.patch.object(inner.subprocess, "run") as run, mock.patch.object(
            inner.time, "sleep"
        ) as sleep:
            run.side_effect = [
                mock.Mock(),
                mock.Mock(stdout="123\n456\n"),
                mock.Mock(stdout="456\n"),
            ]
            inner.retire_previous_job("123", 30)
        self.assertEqual(run.call_args_list[0], mock.call(["scancel", "123"], check=True))
        self.assertEqual(run.call_count, 3)
        sleep.assert_called_once_with(1)

    def test_retires_before_start_only_for_sidecar_profiles(self):
        for sidecars in (["example"], []):
            with self.subTest(sidecars=sidecars), tempfile.TemporaryDirectory() as tmp:
                events = []
                cfg = {"profiles": {"cpu": {"sidecars": sidecars}}}
                argv = ["cs", "--config", "unused", "--profile", "cpu",
                        "--session-dir", tmp, "--run-log", tmp + "/run.log",
                        "--tunnel-log", tmp + "/tunnel.log", "--previous-job-id", "123",
                        "--test-command", "true"]
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(inner, "load_config", return_value=cfg),
                    mock.patch.object(inner, "merged_env", return_value={}),
                    mock.patch.object(inner, "retire_previous_job",
                                      side_effect=lambda *a: events.append("retire")),
                    mock.patch.object(inner, "start_sidecars",
                                      side_effect=lambda *a: events.append("start") or []),
                    mock.patch.object(inner, "supervise_pty_output", return_value=0) as supervise,
                ):
                    self.assertEqual(inner.main(), 0)
                self.assertEqual(events, ["retire", "start"] if sidecars else ["start"])
                self.assertEqual(supervise.call_args.args[3], None if sidecars else "123")

    def test_rejects_process_that_exits_during_readiness_check(self):
        cfg = {
            "profiles": {"cpu": {"sidecars": ["example"]}},
            "sidecars": {"example": {"command": "false", "log": "example.log",
                                     "ready_command": "true", "ready_timeout": "5s"}},
        }
        process = mock.Mock(returncode=1)
        process.poll.return_value = 1
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(inner.subprocess, "Popen", return_value=process),
            mock.patch.object(inner.subprocess, "run", return_value=mock.Mock(returncode=0)),
        ):
            with self.assertRaisesRegex(RuntimeError, "exited with status 1"):
                inner.start_sidecars(cfg, "cpu", pathlib.Path(tmp), os.environ.copy())
