import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


MOCK_EXECUTABLE = '''import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["TGPROXY_TEST_STATE_PATH"])
state = json.loads(state_path.read_text())
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
state["calls"].append([command, *arguments])
exit_code = 0
if command == "launchctl":
    operation = arguments[0]
    if operation == "print":
        if not state["loaded"]:
            exit_code = 1
        elif state.get("pid"):
            print("    pid = " + str(state["pid"]))
    elif operation == "bootstrap":
        assert arguments[1] == "gui/" + str(os.getuid())
        assert Path(arguments[2]).is_file()
        if state.get("bootstrap_failure"):
            exit_code = 1
        else:
            state["loaded"] = True
    elif operation == "bootout":
        if state.get("bootout_failure"):
            exit_code = 1
        else:
            state["loaded"] = False
    else:
        raise AssertionError(arguments)
elif command == "lsof":
    assert arguments == ["-nP", "-a", "-p", str(state["pid"]), "-iTCP@127.0.0.1:1080", "-sTCP:LISTEN", "-t"]
    exit_code = 0 if state["listening"] else 1
elif command == "sleep":
    state["sleeps"] = state.get("sleeps", 0) + 1
    if state.get("readiness_delay") == state["sleeps"]:
        state["listening"] = True
else:
    raise AssertionError(command)
state_path.write_text(json.dumps(state))
sys.exit(exit_code)
'''


class TgproxyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="tgproxy-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory_path = Path(self.temporary_directory.name)
        self.home_path = self.directory_path / "home with spaces"
        configuration_path = self.home_path / ".config/tgproxy/agent.plist"
        configuration_path.parent.mkdir(parents=True)
        configuration_path.write_text("test plist")
        self.state_path = self.directory_path / "state.json"
        self.write_state(loaded=False, pid=1234, listening=True, calls=[])
        script = Path(__file__).parent.parent.joinpath("src/tgproxy.sh").read_text().replace("@listen_address@", "127.0.0.1:1080")
        for command in ("launchctl", "lsof", "sleep"):
            executable_path = self.directory_path / command
            executable_path.write_text("#!" + sys.executable + "\n" + MOCK_EXECUTABLE)
            executable_path.chmod(0o755)
            script = script.replace("@" + command + "@", shlex.quote(str(executable_path)))
        for command in ("id", "mkdir", "tail", "sed"):
            script = script.replace("@" + command + "@", shlex.quote(shutil.which(command)))
        self.script_path = self.directory_path / "tgproxy.sh"
        self.script_path.write_text(script.replace("$HOME", str(self.home_path)))

    def write_state(self, **updates):
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        state.update(updates)
        self.state_path.write_text(json.dumps(state))

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def run_command(self, *arguments):
        return subprocess.run(
            [shutil.which("bash"), str(self.script_path), *arguments],
            env=dict(os.environ, TGPROXY_TEST_STATE_PATH=str(self.state_path)),
            capture_output=True,
            text=True,
            timeout=8,
        )

    def operation_count(self, operation):
        return sum(call[:2] == ["launchctl", operation] for call in self.read_state()["calls"])

    def test_default_status_is_stopped(self):
        result = self.run_command()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "tgproxy: stopped")

    def test_start_waits_for_own_listener(self):
        self.write_state(listening=False, readiness_delay=2)
        result = self.run_command("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PID 1234, socks5://127.0.0.1:1080", result.stdout)
        self.assertEqual(self.operation_count("bootstrap"), 1)
        self.assertEqual(self.read_state()["sleeps"], 2)
        self.assertTrue((self.home_path / "Library/Logs").is_dir())

    def test_repeated_start_does_not_duplicate_service(self):
        self.assertEqual(self.run_command("on").returncode, 0)
        self.assertEqual(self.run_command("on").returncode, 0)
        self.assertEqual(self.operation_count("bootstrap"), 1)

    def test_running_status(self):
        self.write_state(loaded=True)
        result = self.run_command("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("running", result.stdout)

    def test_process_without_listener_is_not_reported_ready(self):
        self.write_state(loaded=True, listening=False)
        result = self.run_command("status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("starting or restarting", result.stdout)

    def test_service_without_process_is_not_reported_ready(self):
        self.write_state(loaded=True, pid=None)
        result = self.run_command("status")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(any(call[0] == "lsof" for call in self.read_state()["calls"]))

    def test_startup_timeout_is_bounded(self):
        self.write_state(listening=False)
        result = self.run_command("on")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read_state()["sleeps"], 20)
        self.assertIn("tgproxy logs", result.stdout)

    def test_bootstrap_failure_is_propagated(self):
        self.write_state(bootstrap_failure=True)
        self.assertNotEqual(self.run_command("on").returncode, 0)
        self.assertFalse(self.read_state()["loaded"])

    def test_stop_and_repeated_stop(self):
        self.write_state(loaded=True)
        self.assertEqual(self.run_command("off").returncode, 0)
        self.assertEqual(self.run_command("off").returncode, 0)
        self.assertEqual(self.operation_count("bootout"), 1)
        self.assertFalse(self.read_state()["loaded"])

    def test_stop_failure_is_propagated(self):
        self.write_state(loaded=True, bootout_failure=True)
        result = self.run_command("off")
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed to stop", result.stderr)

    def test_logs_before_first_start(self):
        result = self.run_command("logs")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no logs yet", result.stdout)

    def test_logs_show_last_fifty_lines(self):
        log_path = self.home_path / "Library/Logs/tgproxy.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("".join(f"line {index}\n" for index in range(60)))
        result = self.run_command("logs")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), [f"line {index}" for index in range(10, 60)])

    def test_invalid_arguments_do_not_change_service(self):
        for arguments in (("invalid",), ("on", "extra")):
            with self.subTest(arguments=arguments):
                result = self.run_command(*arguments)
                self.assertEqual(result.returncode, 2)
        self.assertEqual(self.read_state()["calls"], [])

    def test_help_does_not_change_service(self):
        self.assertEqual(self.run_command("--help").returncode, 0)
        self.assertEqual(self.read_state()["calls"], [])

    def test_shellcheck(self):
        result = subprocess.run(
            [shutil.which("shellcheck"), "--shell=bash", str(self.script_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
