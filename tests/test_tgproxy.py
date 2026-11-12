import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


MOCK_EXECUTABLE = """import fcntl
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["TGPROXY_TEST_STATE_PATH"])
log_path = Path(os.environ["TGPROXY_TEST_LOG_PATH"])
state_lock = open(str(state_path) + ".lock", "w")
fcntl.flock(state_lock, fcntl.LOCK_EX)
state = json.loads(state_path.read_text())
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
state["calls"].append([command, *arguments])
exit_code = 0

def append_log(lines):
    if lines:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as log_file:
            log_file.write("".join(line + "\\n" for line in lines))

if command == "launchctl":
    operation = arguments[0]
    if operation == "print":
        if not state["loaded"]:
            exit_code = 1
        else:
            print("    program = " + state.get("loaded_program", "/runtime/tgproxy-agent"))
            if state.get("pid"):
                print("    pid = " + str(state["pid"]))
    elif operation == "bootstrap":
        assert arguments[1] == "gui/" + str(os.getuid())
        assert Path(arguments[2]).is_file()
        if state.get("bootstrap_failure"):
            print("Bootstrap failed: 5: Input/output error")
            exit_code = 1
        else:
            state["loaded"] = True
            state["loaded_program"] = state.get("configured_program", "/runtime/tgproxy-agent")
            append_log(state.get("bootstrap_log_lines", []))
    elif operation == "bootout":
        if state.get("bootout_failure"):
            exit_code = 1
        else:
            state["loaded"] = False
            append_log(state.get("bootout_log_lines", []))
    else:
        raise AssertionError(arguments)
elif command == "plutil":
    assert arguments[:5] == ["-extract", "ProgramArguments.0", "raw", "-o", "-"], arguments
    if state.get("configured_program", "") is None:
        exit_code = 1
    else:
        print(state.get("configured_program", "/runtime/tgproxy-agent"))
elif command == "pgrep":
    assert arguments == ["-P", str(state["pid"])]
    for child_pid in state.get("child_pids", []):
        print(child_pid)
    exit_code = 0 if state.get("child_pids") else 1
elif command == "lsof":
    expected_pids = ",".join(str(pid) for pid in [state["pid"], *state.get("child_pids", [])])
    if arguments[5] == "-sTCP:ESTABLISHED":
        assert arguments == ["-nP", "-a", "-p", expected_pids, "-iTCP@127.0.0.1:1080", "-sTCP:ESTABLISHED", "-F", "n"], arguments
        print("p" + str(state["pid"]))
        for index in range(state.get("client_connections", 0)):
            print("n127.0.0.1:1080->127.0.0.1:" + str(50000 + index))
    else:
        assert arguments == ["-nP", "-a", "-p", expected_pids, "-iTCP@127.0.0.1:1080", "-sTCP:LISTEN", "-t"], arguments
        exit_code = 0 if state["listening"] else 1
elif command == "sleep":
    state["sleeps"] = state.get("sleeps", 0) + 1
    if state.get("readiness_delay") == state["sleeps"]:
        state["listening"] = True
else:
    raise AssertionError(command)
state_path.write_text(json.dumps(state))
sys.exit(exit_code)
"""

ELAPSED_PREFIX = re.compile(r"^  +\d+\.\ds ")


def timestamped(text):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z") + " tgproxy agent: " + text


class TgproxyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="tgproxy-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory_path = Path(self.temporary_directory.name)
        self.home_path = self.directory_path / "home with spaces"
        configuration_path = self.home_path / ".config/tgproxy/agent.plist"
        configuration_path.parent.mkdir(parents=True)
        configuration_path.write_text("test plist")
        self.log_path = self.home_path / "Library/Logs/tgproxy.log"
        self.agent_state_path = self.home_path / ".local/state/tgproxy/agent.state"
        self.state_path = self.directory_path / "state.json"
        self.write_state(loaded=False, pid=1234, listening=True, calls=[])
        script = Path(__file__).parent.parent.joinpath("src/tgproxy.sh").read_text()
        script = script.replace("@listen_address@", "127.0.0.1:1080").replace("@state_path@", str(self.agent_state_path))
        script = script.replace("start_timeout_s=90", "start_timeout_s=10").replace("stop_timeout_s=30", "stop_timeout_s=10")
        for command in ("launchctl", "lsof", "pgrep", "sleep", "plutil"):
            executable_path = self.directory_path / command
            executable_path.write_text("#!" + sys.executable + "\n" + MOCK_EXECUTABLE)
            executable_path.chmod(0o755)
            script = script.replace("@" + command + "@", shlex.quote(str(executable_path)))
        for command in ("id", "mkdir", "tail", "sed", "wc", "grep"):
            script = script.replace("@" + command + "@", shlex.quote(shutil.which(command)))
        self.script_path = self.directory_path / "tgproxy.sh"
        self.script_path.write_text(script.replace("$HOME", str(self.home_path)))

    def write_state(self, **updates):
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        state.update(updates)
        self.state_path.write_text(json.dumps(state))

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def write_agent_state(self, **fields):
        values = dict(process_id="1234", mode="tunnelled", mode_start_ts=str(int(time.time()) - 7512), ssh_host="jump", upstream_address="203.0.113.7:3128", next_tunnel_retry_ts="")
        values.update(fields)
        self.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_state_path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))

    def run_command(self, *arguments):
        return subprocess.run(
            [shutil.which("bash"), str(self.script_path), *arguments],
            env=dict(os.environ, TGPROXY_TEST_STATE_PATH=str(self.state_path), TGPROXY_TEST_LOG_PATH=str(self.log_path)),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def operation_count(self, operation):
        return sum(call[:2] == ["launchctl", operation] for call in self.read_state()["calls"])

    def launchctl_operations(self):
        return [call[1] for call in self.read_state()["calls"] if call[0] == "launchctl" and call[1] in ("bootstrap", "bootout")]

    def without_elapsed(self, output):
        return [ELAPSED_PREFIX.sub("", line) for line in output.splitlines()]

    def test_default_status_is_stopped(self):
        result = self.run_command()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "tgproxy: stopped\n")

    def test_start_shows_agent_steps_until_endpoint_listens(self):
        self.log_path.parent.mkdir(parents=True)
        self.log_path.write_text(timestamped("socks5 endpoint listening on socks5://127.0.0.1:1080 (previous run)") + "\n")
        self.write_state(listening=False, bootstrap_log_lines=[
            timestamped("starting (forwarding through an ssh tunnel via jump to 203.0.113.7:3128)"),
            timestamped("starting ssh tunnel via jump"),
            "kex_exchange_identification: read: Connection reset by peer",
            timestamped("ssh tunnel exited before listening"),
            '{"level":"info","msg":"127.0.0.1:5000 <> 127.0.0.1:1080"}',
            '{"level":"error","msg":"dial tcp: connection refused"}',
            timestamped("socks5 endpoint listening on socks5://127.0.0.1:1080 (directly to 203.0.113.7:3128, not wrapped in ssh)"),
        ])
        result = self.run_command("on")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.without_elapsed(result.stdout), [
            "tgproxy: starting",
            "· starting (forwarding through an ssh tunnel via jump to 203.0.113.7:3128)",
            "· starting ssh tunnel via jump",
            "! ssh: kex_exchange_identification: read: Connection reset by peer",
            "! ssh tunnel exited before listening",
            "! gost: dial tcp: connection refused",
            "✓ socks5 endpoint listening on socks5://127.0.0.1:1080 (directly to 203.0.113.7:3128, not wrapped in ssh)",
        ])
        self.assertNotIn("\033", result.stdout)
        self.assertEqual(self.operation_count("bootstrap"), 1)

    def test_start_detects_listener_of_agent_that_does_not_log_it(self):
        result = self.run_command("on")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.without_elapsed(result.stdout), ["tgproxy: starting", "✓ socks5 endpoint listening on socks5://127.0.0.1:1080"])
        self.assertTrue((self.home_path / "Library/Logs").is_dir())

    def test_start_while_starting_follows_progress_without_new_service(self):
        self.write_state(loaded=True, listening=False)
        self.script_path.write_text(self.script_path.read_text().replace("start_timeout_s=10", "start_timeout_s=1"))
        result = self.run_command("on")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.splitlines()[0], "tgproxy: already starting; following progress")
        self.assertIn("tgproxy: still starting after 1s; it continues in the background (tgproxy status, tgproxy logs --follow)", result.stdout)
        self.assertEqual(self.operation_count("bootstrap"), 0)

    def test_repeated_start_does_not_duplicate_service(self):
        self.assertEqual(self.run_command("on").returncode, 0)
        result = self.run_command("on")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines()[0], "tgproxy: running")
        self.assertEqual(self.operation_count("bootstrap"), 1)

    def test_bootstrap_failure_is_propagated(self):
        self.write_state(bootstrap_failure=True)
        result = self.run_command("on")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tgproxy: launchctl bootstrap failed with exit code 1: Bootstrap failed: 5: Input/output error", result.stdout)
        self.assertFalse(self.read_state()["loaded"])

    def test_running_status_shows_tunnelled_upstream_uptime_and_clients(self):
        self.write_state(loaded=True, client_connections=3)
        self.write_agent_state()
        result = self.run_command("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:3], ["tgproxy: running", "  endpoint socks5://127.0.0.1:1080", "  upstream through the ssh tunnel via jump to 203.0.113.7:3128"])
        self.assertRegex(lines[3], r"^  up       2h 05m \(since \d\d:\d\d:\d\d\)$")
        self.assertEqual(lines[4:], ["  clients  3 connections", "  PID      1234"])

    def test_running_status_in_direct_mode_shows_next_tunnel_retry(self):
        self.write_state(loaded=True)
        self.write_agent_state(mode="direct", next_tunnel_retry_ts=str(int(time.time()) + 130))
        result = self.run_command("status")
        self.assertRegex(result.stdout, r"  upstream directly to 203\.0\.113\.7:3128, not wrapped in ssh \(ssh tunnel via jump is unavailable; retrying the tunnel in 2m \d\ds\)\n")
        self.assertIn("  clients  0 connections\n", result.stdout)

    def test_status_ignores_state_of_another_agent_process(self):
        self.write_state(loaded=True)
        self.write_agent_state(process_id="999")
        result = self.run_command("status")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("upstream", result.stdout)
        self.assertNotIn("  up ", result.stdout)

    def test_listener_owned_by_child_process_counts(self):
        self.write_state(loaded=True, child_pids=[1240, 1241])
        result = self.run_command("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("running", result.stdout)

    def test_starting_status_shows_last_step(self):
        self.write_state(loaded=True, listening=False)
        self.log_path.parent.mkdir(parents=True)
        self.log_path.write_text("2026-11-12T13:05:00+0300 tgproxy agent: retrying ssh tunnel in 5s\n" + '{"level":"info","msg":"x"}\n')
        result = self.run_command("status")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(), ["tgproxy: starting or restarting (PID 1234)", "  last step  13:05:00 retrying ssh tunnel in 5s", "  follow progress: tgproxy logs --follow"])

    def test_service_without_process_is_not_reported_ready(self):
        self.write_state(loaded=True, pid=None)
        result = self.run_command("status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("starting (agent loaded, process not running yet)", result.stdout)
        self.assertFalse(any(call[0] == "lsof" for call in self.read_state()["calls"]))

    def test_stop_shows_agent_steps(self):
        self.write_state(loaded=True, bootout_log_lines=[timestamped("stop requested (SIGTERM); stopping gost and the ssh tunnel"), timestamped("stopped")])
        result = self.run_command("off")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.without_elapsed(result.stdout), ["tgproxy: stopping", "· stop requested (SIGTERM); stopping gost and the ssh tunnel", "✓ stopped", "tgproxy: stopped"])
        repeated_result = self.run_command("off")
        self.assertEqual((repeated_result.returncode, repeated_result.stdout), (0, "tgproxy: already stopped\n"))
        self.assertEqual(self.operation_count("bootout"), 1)

    def test_stop_failure_is_propagated(self):
        self.write_state(loaded=True, bootout_failure=True)
        result = self.run_command("off")
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed to stop", result.stderr)

    def test_status_warns_about_outdated_agent(self):
        self.write_state(loaded=True, loaded_program="/old/tgproxy-agent", configured_program="/new/tgproxy-agent")
        result = self.run_command("status")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines()[-1], "  ! the running agent is outdated (loaded before the last configuration switch); restart it with: tgproxy restart")

    def test_unreadable_configuration_is_not_reported_as_outdated(self):
        self.write_state(loaded=True, configured_program=None)
        self.assertNotIn("outdated", self.run_command("status").stdout)

    def test_start_restarts_outdated_agent(self):
        self.write_state(loaded=True, loaded_program="/old/tgproxy-agent", configured_program="/new/tgproxy-agent")
        result = self.run_command("on")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "tgproxy: the running agent is outdated (loaded before the last configuration switch); restarting it")
        self.assertEqual(self.launchctl_operations(), ["bootout", "bootstrap"])
        self.assertEqual(self.read_state()["loaded_program"], "/new/tgproxy-agent")

    def test_restart(self):
        self.write_state(loaded=True)
        self.assertEqual(self.run_command("restart").returncode, 0)
        self.assertEqual(self.launchctl_operations(), ["bootout", "bootstrap"])

    def test_restart_of_stopped_agent_only_starts_it(self):
        self.assertEqual(self.run_command("restart").returncode, 0)
        self.assertEqual(self.launchctl_operations(), ["bootstrap"])

    def test_refresh_restarts_only_outdated_agent(self):
        result = self.run_command("refresh")
        self.assertEqual((result.returncode, result.stdout), (0, "tgproxy: not running; nothing to refresh\n"))
        self.write_state(loaded=True)
        result = self.run_command("refresh")
        self.assertEqual((result.returncode, result.stdout), (0, "tgproxy: agent is up to date\n"))
        self.write_state(configured_program="/new/tgproxy-agent")
        result = self.run_command("refresh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.launchctl_operations(), ["bootout", "bootstrap"])

    def test_logs_before_first_start(self):
        result = self.run_command("logs")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no logs yet", result.stdout)

    def test_logs_show_last_fifty_agent_lines_and_current_status(self):
        self.log_path.parent.mkdir(parents=True)
        lines = []
        for index in range(60):
            lines.append(f"line {index}")
            lines.append('{"level":"info","msg":"connection ' + str(index) + '"}')
        lines.append('{"level":"error","msg":"dial failed"}')
        self.log_path.write_text("\n".join(lines) + "\n")
        result = self.run_command("logs")
        self.assertEqual(result.returncode, 0)
        output_lines = result.stdout.splitlines()
        self.assertEqual(output_lines[:50], [f"line {index}" for index in range(11, 60)] + ['{"level":"error","msg":"dial failed"}'])
        self.assertRegex(output_lines[50], r"^--- now \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} ---$")
        self.assertEqual(output_lines[51:], ["tgproxy: stopped"])

    def test_logs_follow_uses_file_reopen(self):
        self.log_path.parent.mkdir(parents=True)
        self.log_path.write_text("line\n")
        arguments_path = self.directory_path / "tail-arguments.json"
        tail_path = self.directory_path / "tail"
        tail_path.write_text("#!" + sys.executable + "\nimport json, sys\nfrom pathlib import Path\nPath(" + repr(str(arguments_path)) + ").write_text(json.dumps(sys.argv[1:]))\n")
        tail_path.chmod(0o755)
        self.script_path.write_text(self.script_path.read_text().replace(shlex.quote(shutil.which("tail")), shlex.quote(str(tail_path))))
        result = self.run_command("logs", "--follow")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(arguments_path.read_text()), ["-n", "50", "-F", str(self.log_path)])

    def test_invalid_arguments_do_not_change_service(self):
        for arguments in (("invalid",), ("on", "extra"), ("logs", "invalid"), ("refresh", "now")):
            with self.subTest(arguments=arguments):
                result = self.run_command(*arguments)
                self.assertEqual(result.returncode, 2)
        self.assertEqual(self.read_state()["calls"], [])

    def test_help_does_not_change_service(self):
        result = self.run_command("--help")
        self.assertEqual((result.returncode, result.stdout), (0, "usage: tgproxy [on|off|restart|refresh|status|logs [--follow]]\n"))
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
