import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


MOCK_EXECUTABLE = '''import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time

state_path = Path(os.environ["TGPROXY_TEST_STATE_PATH"])
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]

@contextlib.contextmanager
def locked_state():
    with open(state_path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text())
        yield state
        temporary_path = state_path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(state))
        os.replace(temporary_path, state_path)

def update_state(**updates):
    with locked_state() as state:
        state.update(updates)
    return state

def record_call():
    with locked_state() as state:
        state["calls"].append([command, *arguments])
    return state

def run_until_terminated():
    def on_terminate(signal_number, frame):
        update_state(**{command + "_terminated": True})
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_terminate)
    while True:
        time.sleep(0.05)

state = record_call()
if command == "ssh":
    if len([call for call in state["calls"] if call[0] == "ssh"]) <= state.get("ssh_failure_count", 0):
        sys.exit(255)
    update_state(ssh_pid=os.getpid())
    run_until_terminated()
elif command == "lsof":
    if arguments[4] == "-iTCP@127.0.0.1:1080":
        assert arguments == ["-nP", "-a", "-p", arguments[3], "-iTCP@127.0.0.1:1080", "-sTCP:LISTEN", "-t"], arguments
        sys.exit(0 if state.get("gost_listening", True) and str(state.get("gost_pid")) == arguments[3] else 1)
    assert arguments == ["-nP", "-a", "-p", str(state.get("ssh_pid")), "-iTCP@127.0.0.1:15432", "-sTCP:LISTEN", "-t"], arguments
    sys.exit(0 if state["tunnel_listening"] else 1)
elif command == "sleep":
    state = update_state(sleeps=state.get("sleeps", 0) + 1)
    if state.get("readiness_delay") == state["sleeps"]:
        update_state(tunnel_listening=True)
    if float(arguments[0]) >= 5:
        time.sleep(state.get("retry_sleep_duration_s", 0))
elif command == "gost":
    if state.get("gost_failure"):
        sys.exit(1)
    update_state(gost_pid=os.getpid())
    run_until_terminated()
else:
    raise AssertionError(command)
'''


DIRECT_UPSTREAM_URL = "http://user:secret@203.0.113.7:3128"
TUNNELLED_UPSTREAM_URL = "http://user:secret@127.0.0.1:15432"


class TgproxyAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="tgproxy-agent-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory_path = Path(self.temporary_directory.name)
        self.state_path = self.directory_path / "state.json"
        self.write_state(tunnel_listening=True, calls=[])
        script = Path(__file__).parent.parent.joinpath("src/agent.sh").read_text()
        self.agent_state_path = self.directory_path / "agent state/agent.state"
        for placeholder, value in dict(self.placeholders(), state_path=str(self.agent_state_path)).items():
            script = script.replace("@" + placeholder + "@", value)
        for command in ("mkdir", "mv", "sed", "rm"):
            script = script.replace("@" + command + "@", shlex.quote(shutil.which(command)))
        for command in ("ssh", "lsof", "sleep", "gost"):
            executable_path = self.directory_path / command
            executable_path.write_text("#!" + sys.executable + "\n" + MOCK_EXECUTABLE)
            executable_path.chmod(0o755)
            script = script.replace("@" + command + "@", shlex.quote(str(executable_path)))
        self.script_path = self.directory_path / "agent.sh"
        self.script_path.write_text(script)

    tunnel_disabled = False

    def placeholders(self):
        if self.tunnel_disabled:
            return {
                "listen_address": "127.0.0.1:1080",
                "gost_upstream_url": "http://user:secret@203.0.113.7:3128",
                "direct_upstream_url": "http://user:secret@203.0.113.7:3128",
                "tunnel_ssh_host": "",
                "tunnel_target_address": "203.0.113.7:3128",
                "tunnel_local_port": "15432",
            }
        return {
            "listen_address": "127.0.0.1:1080",
            "gost_upstream_url": "http://user:secret@127.0.0.1:15432",
            "direct_upstream_url": "http://user:secret@203.0.113.7:3128",
            "tunnel_ssh_host": "jump",
            "tunnel_target_address": "203.0.113.7:3128",
            "tunnel_local_port": "15432",
        }

    def write_state(self, **updates):
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        state.update(updates)
        self.state_path.write_text(json.dumps(state))

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def start_agent(self):
        process = subprocess.Popen(
            [shutil.which("bash"), str(self.script_path)],
            env=dict(os.environ, TGPROXY_TEST_STATE_PATH=str(self.state_path)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.kill_process_group, process)
        return process

    def kill_process_group(self, process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.communicate(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def wait_for_calls(self, command, timeout_s=5):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if any(call[0] == command for call in self.read_state()["calls"]):
                return
            time.sleep(0.05)
        self.fail(f"{command} was not called")

    def calls_of(self, command):
        return [call for call in self.read_state()["calls"] if call[0] == command]

    def test_gost_starts_after_tunnel_listens(self):
        self.write_state(tunnel_listening=False, readiness_delay=2)
        process = self.start_agent()
        self.wait_for_calls("gost")
        self.assertEqual(len([call for call in self.calls_of("sleep") if call[1] == "1"]), 2)
        self.assertEqual(
            self.calls_of("gost")[0][1:],
            ["-L", "socks5://127.0.0.1:1080", "-F", "http://user:secret@127.0.0.1:15432"],
        )
        ssh_arguments = self.calls_of("ssh")[0][1:]
        self.assertIn("-L", ssh_arguments)
        self.assertEqual(ssh_arguments[ssh_arguments.index("-L") + 1], "127.0.0.1:15432:203.0.113.7:3128")
        self.assertEqual(ssh_arguments[-1], "jump")
        self.assertIn("BatchMode=yes", ssh_arguments)
        self.assertIn("ExitOnForwardFailure=yes", ssh_arguments)
        self.assertIn("ConnectTimeout=15", ssh_arguments)
        self.assertIsNone(process.poll())

    def test_agent_stops_children_and_exits_when_terminated(self):
        process = self.start_agent()
        self.wait_for_calls("gost")
        process.terminate()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (self.read_state().get("ssh_terminated") and self.read_state().get("gost_terminated")):
            time.sleep(0.05)
        self.assertTrue(self.read_state().get("ssh_terminated"))
        self.assertTrue(self.read_state().get("gost_terminated"))

    def test_gost_exit_stops_tunnel_and_fails(self):
        self.write_state(gost_failure=True)
        process = self.start_agent()
        _, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 1)
        self.assertIn("child process exited", stderr)
        self.assertTrue(self.read_state().get("ssh_terminated"))

    def wait_for_call_count(self, command, count, timeout_s=30):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len(self.calls_of(command)) >= count:
                return
            time.sleep(0.05)
        self.fail(f"{command} was not called {count} times")

    def retry_delays(self):
        delays, slept_s, ssh_calls = [], 0, 0
        for call in self.read_state()["calls"]:
            if call[0] == "sleep" and call[1] not in ("1", "0.2"):
                slept_s += int(call[1])
            elif call[0] == "ssh":
                if ssh_calls:
                    delays.append(slept_s)
                slept_s = 0
                ssh_calls += 1
        return delays

    def gost_upstreams(self):
        return [call[-1] for call in self.calls_of("gost")]

    def test_tunnel_failure_falls_back_to_direct_upstream_then_switches_back(self):
        self.write_state(ssh_failure_count=3)
        process = self.start_agent()
        self.wait_for_call_count("ssh", 4)
        self.wait_for_call_count("gost", 2)
        self.assertEqual(self.gost_upstreams(), [DIRECT_UPSTREAM_URL, TUNNELLED_UPSTREAM_URL])
        self.assertEqual(self.retry_delays(), [5, 10, 20])
        self.assertTrue(self.read_state().get("gost_terminated"))
        self.assertIsNone(process.poll())
        process.terminate()
        _, stderr = process.communicate(timeout=5)
        self.assertIn("ssh tunnel exited before listening", stderr)
        self.assertIn("forwarding directly to the upstream proxy", stderr)
        self.assertRegex(stderr, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} tgproxy agent: retrying ssh tunnel in 5s")
        self.assertIn("switching to the tunnelled upstream", stderr)

    def test_tunnel_retry_delay_is_capped(self):
        self.write_state(ssh_failure_count=8)
        process = self.start_agent()
        self.wait_for_call_count("ssh", 9, timeout_s=60)
        self.assertEqual(self.retry_delays(), [5, 10, 20, 40, 80, 160, 300, 300])
        self.assertIsNone(process.poll())

    def test_tunnel_readiness_timeout_falls_back_to_direct_upstream(self):
        self.write_state(tunnel_listening=False)
        process = self.start_agent()
        self.wait_for_call_count("ssh", 2)
        self.wait_for_calls("gost")
        self.assertTrue(self.read_state().get("ssh_terminated"))
        self.assertEqual(self.gost_upstreams(), [DIRECT_UPSTREAM_URL])
        self.assertGreaterEqual(self.read_state()["sleeps"], 61)
        process.terminate()
        _, stderr = process.communicate(timeout=5)
        self.assertIn("did not start listening", stderr)

    def test_direct_gost_exit_during_fallback_restarts_agent(self):
        self.write_state(ssh_failure_count=100, gost_failure=True)
        process = self.start_agent()
        _, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 1)
        self.assertIn("forwarding directly to the upstream proxy", stderr)
        self.assertIn("child process exited", stderr)

    def test_without_tunnel_runs_gost_directly(self):
        self.tunnel_disabled = True
        self.setUp()
        process = self.start_agent()
        self.wait_for_calls("gost")
        self.assertEqual(self.calls_of("ssh"), [])
        self.assertEqual(
            self.calls_of("gost")[0][1:],
            ["-L", "socks5://127.0.0.1:1080", "-F", "http://user:secret@203.0.113.7:3128"],
        )
        self.assertIsNone(process.poll())

    def test_without_tunnel_gost_exit_fails(self):
        self.tunnel_disabled = True
        self.setUp()
        self.write_state(gost_failure=True)
        process = self.start_agent()
        _, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 1)
        self.assertIn("child process exited", stderr)

    def wait_for_log(self, process, text, timeout_s=10):
        deadline = time.monotonic() + timeout_s
        collected = ""
        file_descriptor = process.stderr.fileno()
        os.set_blocking(file_descriptor, False)
        while time.monotonic() < deadline:
            try:
                chunk = os.read(file_descriptor, 65536)
            except BlockingIOError:
                chunk = b""
            collected += chunk.decode(errors="replace")
            if text in collected:
                return collected
            time.sleep(0.05)
        self.fail(f"log does not contain {text!r}: {collected}")

    def remaining_log(self, process, collected):
        os.set_blocking(process.stderr.fileno(), True)
        _, stderr = process.communicate(timeout=5)
        return collected + stderr

    def read_agent_state(self):
        return dict(line.split("=", 1) for line in self.agent_state_path.read_text().splitlines())

    def test_tunnelled_start_logs_each_step_and_records_state(self):
        process = self.start_agent()
        log = self.wait_for_log(process, "socks5 endpoint listening")
        self.assertRegex(log, r"tgproxy agent: starting \(forwarding through an ssh tunnel via jump to 203\.0\.113\.7:3128\)\n.*tgproxy agent: starting ssh tunnel via jump\n.*tgproxy agent: ssh tunnel listening on 127\.0\.0\.1:15432\n.*tgproxy agent: socks5 endpoint listening on socks5://127\.0\.0\.1:1080 \(through the ssh tunnel via jump to 203\.0\.113\.7:3128\)")
        self.assertNotIn("secret", log)
        agent_state = self.read_agent_state()
        self.assertEqual(agent_state["process_id"], str(process.pid))
        self.assertEqual(agent_state["mode"], "tunnelled")
        self.assertEqual(agent_state["ssh_host"], "jump")
        self.assertEqual(agent_state["upstream_address"], "203.0.113.7:3128")
        self.assertEqual(agent_state["next_tunnel_retry_ts"], "")
        self.assertLessEqual(abs(int(agent_state["mode_start_ts"]) - time.time()), 5)

    def test_fallback_records_direct_mode_and_next_tunnel_retry(self):
        self.write_state(ssh_failure_count=100, retry_sleep_duration_s=0.5)
        process = self.start_agent()
        log = self.wait_for_log(process, "retrying ssh tunnel in 5s")
        self.assertIn("socks5 endpoint listening on socks5://127.0.0.1:1080 (directly to 203.0.113.7:3128, not wrapped in ssh)", log)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.read_agent_state().get("next_tunnel_retry_ts", "") == "":
            time.sleep(0.05)
        agent_state = self.read_agent_state()
        self.assertEqual(agent_state["mode"], "direct")
        self.assertGreater(int(agent_state["next_tunnel_retry_ts"]), time.time())

    def test_termination_during_tunnel_retry_wait_is_immediate_and_logged(self):
        self.write_state(ssh_failure_count=100, retry_sleep_duration_s=30)
        process = self.start_agent()
        collected = self.wait_for_log(process, "retrying ssh tunnel in 5s")
        termination_start = time.monotonic()
        os.kill(process.pid, signal.SIGTERM)
        stderr = self.remaining_log(process, collected)
        self.assertLess(time.monotonic() - termination_start, 3)
        self.assertEqual(process.returncode, 0)
        self.assertIn("tgproxy agent: stop requested (SIGTERM); stopping gost and the ssh tunnel", stderr)
        self.assertIn("tgproxy agent: stopped", stderr)
        self.assertTrue(self.read_state().get("gost_terminated"))
        self.assertFalse(self.agent_state_path.exists())

    def test_endpoint_that_does_not_listen_is_reported(self):
        self.write_state(gost_listening=False)
        process = self.start_agent()
        self.wait_for_log(process, "socks5 endpoint is not listening yet on socks5://127.0.0.1:1080")
        self.assertIsNone(process.poll())

    def test_direct_start_logs_upstream(self):
        self.tunnel_disabled = True
        self.setUp()
        process = self.start_agent()
        log = self.wait_for_log(process, "socks5 endpoint listening")
        self.assertIn("tgproxy agent: starting (forwarding directly to 203.0.113.7:3128)", log)
        self.assertIn("socks5 endpoint listening on socks5://127.0.0.1:1080 (directly to 203.0.113.7:3128)", log)
        self.assertEqual(self.read_agent_state()["mode"], "direct")

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
