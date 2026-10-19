import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


MOCK_EXECUTABLE = '''import json
import os
from pathlib import Path
import signal
import sys
import time

state_path = Path(os.environ["TGPROXY_TEST_STATE_PATH"])
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]

def update_state(**updates):
    state = json.loads(state_path.read_text())
    state.update(updates)
    state_path.write_text(json.dumps(state))
    return state

def record_call():
    state = json.loads(state_path.read_text())
    state["calls"].append([command, *arguments])
    state_path.write_text(json.dumps(state))
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
    if state.get("ssh_failure"):
        sys.exit(255)
    update_state(ssh_pid=os.getpid())
    run_until_terminated()
elif command == "lsof":
    assert arguments == ["-nP", "-a", "-p", str(state.get("ssh_pid")), "-iTCP@127.0.0.1:15432", "-sTCP:LISTEN", "-t"], arguments
    sys.exit(0 if state["tunnel_listening"] else 1)
elif command == "sleep":
    state = update_state(sleeps=state.get("sleeps", 0) + 1)
    if state.get("readiness_delay") == state["sleeps"]:
        update_state(tunnel_listening=True)
elif command == "gost":
    if state.get("gost_failure"):
        sys.exit(1)
    run_until_terminated()
else:
    raise AssertionError(command)
'''


class TgproxyAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="tgproxy-agent-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory_path = Path(self.temporary_directory.name)
        self.state_path = self.directory_path / "state.json"
        self.write_state(tunnel_listening=True, calls=[])
        script = Path(__file__).parent.parent.joinpath("src/agent.sh").read_text()
        for placeholder, value in self.placeholders().items():
            script = script.replace("@" + placeholder + "@", value)
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
                "tunnel_ssh_host": "",
                "tunnel_target_address": "203.0.113.7:3128",
                "tunnel_local_port": "15432",
            }
        return {
            "listen_address": "127.0.0.1:1080",
            "gost_upstream_url": "http://user:secret@127.0.0.1:15432",
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
        return subprocess.Popen(
            [shutil.which("bash"), str(self.script_path)],
            env=dict(os.environ, TGPROXY_TEST_STATE_PATH=str(self.state_path)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

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
        self.addCleanup(process.kill)
        self.wait_for_calls("gost")
        self.assertEqual(self.read_state()["sleeps"], 2)
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
        self.assertIsNone(process.poll())

    def test_agent_stops_children_and_exits_when_terminated(self):
        process = self.start_agent()
        self.addCleanup(process.kill)
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
        self.addCleanup(process.kill)
        _, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 1)
        self.assertIn("child process exited", stderr)
        self.assertTrue(self.read_state().get("ssh_terminated"))

    def test_tunnel_exit_before_listening_fails_without_gost(self):
        self.write_state(ssh_failure=True, tunnel_listening=False)
        process = self.start_agent()
        self.addCleanup(process.kill)
        _, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 1)
        self.assertIn("exited before listening", stderr)
        self.assertEqual(self.calls_of("gost"), [])

    def test_tunnel_readiness_timeout_is_bounded(self):
        self.write_state(tunnel_listening=False)
        process = self.start_agent()
        self.addCleanup(process.kill)
        _, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 1)
        self.assertIn("did not start listening", stderr)
        self.assertEqual(self.read_state()["sleeps"], 40)
        self.assertEqual(self.calls_of("gost"), [])
        self.assertTrue(self.read_state().get("ssh_terminated"))

    def test_without_tunnel_runs_gost_directly(self):
        self.tunnel_disabled = True
        self.setUp()
        process = self.start_agent()
        self.addCleanup(process.kill)
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
        self.addCleanup(process.kill)
        _, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 1)
        self.assertIn("child process exited", stderr)

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
