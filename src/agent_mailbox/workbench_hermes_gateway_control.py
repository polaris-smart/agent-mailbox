"""Local, owner-side Hermes Gateway lifecycle evidence for an explicit cutover.

No browser or mail payload may choose the executable, profile, service label or
socket.  This adapter does not decide when a Gateway may be stopped: the
cutover coordinator must first establish the drain and binding gates.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import plistlib
import re
import shlex
import socket
import stat
import subprocess
import time
from pathlib import Path

from .workbench_hermes_catalog import _installed_python
from .workbench_hermes_process import _file
from .workbench_receiver_status import _request


class HermesStopAfterMarkerUnconfirmed(ValueError):
    """The official stop marker may be live although launchd stop failed."""


# Hermes gateway.status tolerates up to 2 seconds of macOS start-time
# fingerprint drift between its retained record and a later psutil read.
_START_TIME_DRIFT_CENTISECONDS = 200


def _service_launcher_matches(command: Path, plist: Path) -> bool:
    """Require the official service to use the same CLI as stop/start.

    A separate installed CLI can control a different Hermes environment even
    when both files are named ``hermes``.  The current supported launchd shape
    has an osascript supervisor and two references to its selected CLI.
    Unknown service shapes fail closed before a cutover requests a drain.
    """
    try:
        info = plist.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            return False
        service = plistlib.loads(plist.read_bytes())
        args = service.get("ProgramArguments")
        if (
            service.get("Label") != "ai.hermes.gateway"
            or not isinstance(args, list)
            or len(args) != 5
            or args[:4] != ["/usr/bin/osascript", "-l", "JavaScript", "-e"]
            or not isinstance(args[4], str)
        ):
            return False
        selected = shlex.quote(str(command))
        return (
            f"exec {selected} --run-module hermes_cli.stderr_timestamp" in args[4]
            and f"-- {selected} gateway run --external-supervisor" in args[4]
        )
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        return False


def _loaded_service_domain(command: Path, plist: Path, profile: Path) -> str | None:
    """Read the loaded launchd job, not just a possibly stale plist on disk.

    launchctl's human-readable output is deliberately parsed narrowly.  Any
    missing/changed field or ambiguous registration refuses a cutover.  Raw
    output can contain inherited environment values and is never logged.
    """
    if os.uname().sysname != "Darwin" or not _service_launcher_matches(command, plist):
        return None
    try:
        service = plistlib.loads(plist.read_bytes())
        expected_args = service["ProgramArguments"]
        expected_env = service["EnvironmentVariables"]
        if (
            service.get("WorkingDirectory") != str(profile)
            or expected_env.get("HERMES_HOME") != str(profile)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in expected_env.items()
            )
        ):
            return None
        found = []
        for kind in ("gui", "user"):
            domain = f"{kind}/{os.getuid()}"
            target = f"{domain}/ai.hermes.gateway"
            result = subprocess.run(
                ["/bin/launchctl", "print", target],
                capture_output=True,
                timeout=5,
                check=False,
                env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
            )
            if result.returncode != 0:
                continue
            if len(result.stdout) > 262144:
                return None
            lines = result.stdout.decode("utf-8", "strict").splitlines()
            if not lines or lines[0] != f"{target} = {{":
                return None

            def field(name, lines=lines):
                prefix = f"\t{name} = "
                values = [line[len(prefix) :] for line in lines if line.startswith(prefix)]
                return values[0] if len(values) == 1 else None

            def block(name, lines=lines):
                header = f"\t{name} = {{"
                starts = [index for index, line in enumerate(lines) if line == header]
                if len(starts) != 1:
                    return None
                start = starts[0] + 1
                try:
                    end = lines.index("\t}", start)
                except ValueError:
                    return None
                entries = lines[start:end]
                return (
                    [line[2:] for line in entries]
                    if all(line.startswith("\t\t") for line in entries)
                    else None
                )

            args = block("arguments")
            env_lines = block("environment")
            if args != expected_args or env_lines is None:
                return None
            environment = {}
            for line in env_lines:
                key, separator, value = line.partition(" => ")
                if not separator or key in environment:
                    return None
                environment[key] = value
            if (
                field("path") != str(plist)
                or field("type") != "LaunchAgent"
                or field("state") != "running"
                or field("program") != expected_args[0]
                or field("working directory") != str(profile)
                or not (field("pid") or "").isdigit()
                or int(field("pid")) <= 0
                or any(environment.get(key) != value for key, value in expected_env.items())
            ):
                return None
            found.append(domain)
        return found[0] if len(found) == 1 else None
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        plistlib.InvalidFileException,
        subprocess.TimeoutExpired,
    ):
        return None


class HermesGatewayControl:
    """Official Hermes drain/CLI operations with independent macOS observations."""

    @classmethod
    def restore_after_pm(cls, profile, domain, *, original_plist_sha256, original_command_sha256):
        """Restore the same service after PM may have published a new venv.

        The old Python selection must not be used after PM publication. Read
        its committed generation afresh, but require the original launcher and
        launchd plist bytes before bootstrapping that exact stopped job.
        """
        profile = Path(profile)
        install = Path.home() / ".hermes/hermes-agent"
        python = _installed_python(profile, install)
        if python is None:
            raise ValueError("hermes_gateway_pm_restore_runtime_unconfirmed")
        control = cls(python, profile, require_stopped=True)
        if (
            not isinstance(original_plist_sha256, str)
            or not isinstance(original_command_sha256, str)
            or original_plist_sha256 != control.plist_digest.hex()
            or original_command_sha256 != control.command_digest.hex()
        ):
            raise ValueError("hermes_gateway_pm_restore_identity_changed")
        control.start_stopped_original(
            domain, plist_sha256=original_plist_sha256, command_sha256=original_command_sha256
        )
        return control

    def __init__(self, python: Path, profile: Path, *, require_stopped=False):
        self.python = Path(python)
        self.profile = Path(profile)
        self.install = Path.home() / ".hermes/hermes-agent"
        self.command = (
            self.install / ".hermes/bin/hermes"
            if (self.install / ".hermes/bin/hermes").exists()
            else self.install / ".venv/bin/hermes"
        )
        self.plist = Path.home() / "Library/LaunchAgents/ai.hermes.gateway.plist"
        if (
            self.profile != Path.home() / ".hermes"
            or self.python != _installed_python(self.profile, self.install)
            or not self.command.is_file()
        ):
            raise ValueError("hermes_gateway_control_scope_unconfirmed")
        self.python_target = _file(self.python, executable=True)
        _file(self.command, executable=True)
        if not _service_launcher_matches(self.command, self.plist):
            raise ValueError("hermes_gateway_service_launcher_unconfirmed")
        if require_stopped is True:
            if not self.service_stopped():
                raise ValueError("hermes_gateway_not_stopped")
            self.domain = None
        elif require_stopped is False:
            self.domain = _loaded_service_domain(self.command, self.plist, self.profile)
            if self.domain is None:
                raise ValueError("hermes_gateway_loaded_service_unconfirmed")
        else:
            raise ValueError("hermes_gateway_control_mode_unconfirmed")
        self.plist_digest = hashlib.sha256(self.plist.read_bytes()).digest()
        self.command_digest = hashlib.sha256(self.command.read_bytes()).digest()

    def _same_installation(self):
        try:
            return (
                self.python == _installed_python(self.profile, self.install)
                and hashlib.sha256(self.plist.read_bytes()).digest() == self.plist_digest
                and hashlib.sha256(self.command.read_bytes()).digest() == self.command_digest
                and _service_launcher_matches(self.command, self.plist)
                and _file(self.python, executable=True) == self.python_target
                and _file(self.command, executable=True) is not None
            )
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def _env(self):
        return {
            "HOME": str(Path.home()),
            "HERMES_HOME": str(self.profile),
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _official(self, source: str, *arguments):
        # The selected runtime and the installed Hermes source are local, not
        # a path or command supplied by the browser.  -I discards ambient
        # PYTHONPATH, CODEX_*, hooks and proxy settings.
        result = subprocess.run(
            [
                str(self.python),
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); " + source,
                str(self.install),
                str(self.profile),
                *arguments,
            ],
            cwd=str(self.install),
            env=self._env(),
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("hermes_official_control_unconfirmed")
        return result.stdout

    def _drain_marker(self, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch(r"plan_[0-9a-f]{32}", plan_id):
            raise ValueError("hermes_drain_plan_unconfirmed")
        marker = self.profile / ".drain_request.json"
        if not marker.exists() and not marker.is_symlink():
            return marker, None
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("hermes_drain_marker_unconfirmed")
        body = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            raise TypeError("hermes_drain_marker_unconfirmed")
        return marker, body

    def request_drain(self, plan_id):
        marker, existing = self._drain_marker(plan_id)
        if existing is not None:
            raise ValueError("hermes_other_drain_in_progress")
        principal = f"agent-mailbox-cutover:{plan_id}"
        self._official(
            "from gateway.drain_control import write_drain_request; "
            "write_drain_request(principal=sys.argv[3], "
            "suppress_notification=False, home=__import__('pathlib').Path(sys.argv[2]))",
            principal,
        )
        _, body = self._drain_marker(plan_id)
        if body is None or body.get("principal") != principal or not marker.is_file():
            raise ValueError("hermes_drain_marker_unconfirmed")

    def clear_drain(self, plan_id):
        marker, body = self._drain_marker(plan_id)
        if body is None:
            return
        if body.get("principal") != f"agent-mailbox-cutover:{plan_id}":
            raise ValueError("hermes_other_drain_in_progress")
        self._official(
            "from gateway.drain_control import clear_drain_request; "
            "clear_drain_request(home=__import__('pathlib').Path(sys.argv[2]))"
        )
        if marker.exists() or marker.is_symlink():
            raise ValueError("hermes_drain_clear_unconfirmed")

    def runtime(self):
        path = self.profile / "gateway_state.json"
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("hermes_gateway_status_unconfirmed")
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise TypeError("hermes_gateway_status_unconfirmed")
        return result

    def live_status(self):
        """Read status answered by the proven Gateway, even before a plugin exists.

        A retained gateway_state.json is not enough for first-contact drain:
        the official control socket must answer from that same PID now.
        """
        if (
            self.domain is None
            or not self._same_installation()
            or _loaded_service_domain(self.command, self.plist, self.profile) != self.domain
        ):
            raise ValueError("hermes_gateway_live_status_unconfirmed")
        runtime = self.runtime()
        if (
            runtime.get("kind") != "hermes-gateway"
            or runtime.get("hermes_home") != str(self.profile)
            or not self.pid_identity(runtime)
        ):
            raise ValueError("hermes_gateway_live_status_unconfirmed")
        output = self._official(
            "import json; from pathlib import Path; "
            "from gateway.control_socket import query_gateway_control; "
            "a=query_gateway_control(Path(sys.argv[2]), 'status', timeout=3); "
            "sys.stdout.write(json.dumps(a if isinstance(a, dict) else {}))"
        )
        if len(output) > 65536:
            raise ValueError("hermes_gateway_live_status_unconfirmed")
        try:
            status = json.loads(output)
        except (ValueError, UnicodeError):
            raise ValueError("hermes_gateway_live_status_unconfirmed") from None
        if (
            not isinstance(status, dict)
            or status.get("protocol") != 1
            or status.get("answering_pid") != runtime.get("pid")
            or status.get("kind") != runtime.get("kind")
            or status.get("hermes_home") != str(self.profile)
            or status.get("start_time") != runtime.get("start_time")
        ):
            raise ValueError("hermes_gateway_live_status_unconfirmed")
        return runtime, status

    def routing(self, endpoint):
        return _request(endpoint, {"operation": "routing_context"})

    def _live_diagnostic_runtime(self):
        if (
            self.domain is None
            or not self._same_installation()
            or _loaded_service_domain(self.command, self.plist, self.profile) != self.domain
        ):
            raise ValueError("hermes_diagnostic_runtime_unconfirmed")
        runtime = self.runtime()
        if (
            runtime.get("kind") != "hermes-gateway"
            or runtime.get("gateway_state") != "running"
            or runtime.get("hermes_home") != str(self.profile)
            or not self.pid_identity(runtime)
        ):
            raise ValueError("hermes_diagnostic_runtime_unconfirmed")
        return runtime

    def _diagnostic_route(self, endpoint, before, *, readiness_seconds, error):
        # register() starts a private server thread after discovery; its first
        # bind may follow the Gateway control-socket ACK. This waits only for
        # local startup, never for mail or an Agent turn.
        deadline = time.monotonic() + readiness_seconds
        while True:
            try:
                after = self._live_diagnostic_runtime()
            except (OSError, ValueError, TypeError, KeyError, TimeoutError):
                raise ValueError(error) from None
            if (after.get("pid"), after.get("start_time")) != (
                before.get("pid"),
                before.get("start_time"),
            ):
                raise ValueError(error)
            try:
                route = self.routing(endpoint)
                health = _request(endpoint, {"operation": "probe"})
            except (OSError, ValueError, TypeError):
                route, health = None, None
            if (
                isinstance(route, dict)
                and route.get("protocol") == "agent-mailbox.hermes-routing.v1"
                and route.get("host_kind") == "hermes"
                and route.get("pid") == after["pid"]
                and route.get("profile_home") == str(self.profile)
                and route.get("routing_home") == str(self.profile)
                and isinstance(route.get("instance_id"), str)
                and bool(route["instance_id"])
                and route.get("session_verified") is False
                and route.get("delivery_verified") is False
                and health
                == {
                    "protocol": "agent-mailbox.host-health.v1",
                    "host_kind": "hermes",
                    "gateway_injection_available": False,
                    "pinned_sessions": 0,
                    "unconfirmed_deliveries": 0,
                    "recorded_turn_receipts": 0,
                }
            ):
                return {
                    "state": "diagnostic_runtime_observed",
                    "runtime_verified": True,
                    "session_verified": False,
                    "delivery_verified": False,
                }
            if time.monotonic() >= deadline:
                raise ValueError(error)
            time.sleep(min(0.1, deadline - time.monotonic()))

    def observe_diagnostic(self, endpoint):
        """Read-only proof of a diagnostic plugin in the current live Gateway."""
        before = self._live_diagnostic_runtime()
        return self._diagnostic_route(
            endpoint, before, readiness_seconds=0, error="hermes_diagnostic_runtime_unconfirmed"
        )

    def reload_diagnostic(self, endpoint, *, readiness_seconds=5.0):
        """Reload a published diagnostic handler in the same proven Gateway.

        This does not admit a plugin or grant injection. A control-socket ACK
        alone is insufficient: the private diagnostic socket must identify the
        same running process and selected profile after the reload.
        """
        if (
            type(readiness_seconds) not in {int, float}
            or not 0 <= readiness_seconds <= 10
            or not self.receiver_socket_absent(endpoint)
        ):
            raise ValueError("hermes_diagnostic_reload_scope_unconfirmed")
        try:
            before = self._live_diagnostic_runtime()
        except (OSError, ValueError, TypeError, KeyError, TimeoutError):
            raise ValueError("hermes_diagnostic_reload_scope_unconfirmed")
        output = self._official(
            "import json; from pathlib import Path; "
            "from gateway.control_socket import reload_gateway_plugins; "
            "a=reload_gateway_plugins(Path(sys.argv[2]), timeout=5); "
            "sys.stdout.write(json.dumps({'reloaded': a.get('reloaded') is True, "
            "'home': a.get('home'), "
            "'plugin_found': 'agent-mailbox-host' in a.get('plugins', [])}) "
            "if isinstance(a, dict) else '{}')"
        )
        try:
            answer = json.loads(output)
        except (ValueError, UnicodeError):
            raise ValueError("hermes_diagnostic_reload_unconfirmed") from None
        if (
            answer != {"reloaded": True, "home": str(self.profile), "plugin_found": True}
            or not self._same_installation()
            or _loaded_service_domain(self.command, self.plist, self.profile) != self.domain
        ):
            raise ValueError("hermes_diagnostic_reload_unconfirmed")
        return self._diagnostic_route(
            endpoint,
            before,
            readiness_seconds=readiness_seconds,
            error="hermes_diagnostic_reload_unconfirmed",
        )

    def pid_identity(self, runtime):
        pid, recorded = runtime.get("pid"), runtime.get("start_time")
        if type(pid) is not int or pid <= 0 or type(recorded) is not int or recorded <= 0:
            return False
        result = subprocess.run(
            [
                str(self.python),
                "-I",
                "-B",
                "-c",
                "import psutil,sys; print(round(psutil.Process(int(sys.argv[1])).create_time()*100))",
                str(pid),
            ],
            cwd=str(self.install),
            env=self._env(),
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return False
        observed = result.stdout.strip()
        if not observed.isdigit():
            return False
        return abs(int(observed) - recorded) <= _START_TIME_DRIFT_CENTISECONDS

    def original_pid_gone(self, pid):
        if type(pid) is not int or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def service_stopped(self):
        if os.uname().sysname != "Darwin":
            return False
        targets = (
            f"gui/{os.getuid()}/ai.hermes.gateway",
            f"user/{os.getuid()}/ai.hermes.gateway",
            "system/ai.hermes.gateway",
        )
        for target in targets:
            result = subprocess.run(
                ["/bin/launchctl", "print", target],
                capture_output=True,
                timeout=5,
                check=False,
                env=self._env(),
            )
            output = result.stdout + result.stderr
            if result.returncode not in {3, 113} or b"Could not find service" not in output:
                return False
        return True

    def receiver_socket_absent(self, endpoint):
        path = Path(endpoint)
        return not path.exists() and not path.is_symlink()

    def remove_stale_receiver_socket(self, endpoint, *, original_pid_gone, service_stopped):
        """Remove only a private, owner-owned socket that refuses connections."""
        if original_pid_gone is not True or service_stopped is not True:
            raise ValueError("hermes_stale_socket_stop_unconfirmed")
        path = Path(endpoint)
        parent = path.parent
        pinfo, info = parent.lstat(), path.lstat()
        if (
            parent.resolve() != parent
            or not stat.S_ISDIR(pinfo.st_mode)
            or pinfo.st_uid != os.getuid()
            or pinfo.st_mode & 0o077
            or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("hermes_stale_socket_scope_unconfirmed")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(path))
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise ValueError("hermes_stale_socket_unconfirmed") from None
            else:
                raise ValueError("hermes_receiver_still_live")
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_mode, current.st_uid) != (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
        ):
            raise ValueError("hermes_stale_socket_changed")
        path.unlink()

    def stop(self):
        if (
            self.domain is None
            or not self._same_installation()
            or _loaded_service_domain(self.command, self.plist, self.profile) != self.domain
        ):
            raise ValueError("hermes_gateway_loaded_service_unconfirmed")
        running = self.runtime()
        if not self.pid_identity(running):
            raise ValueError("hermes_gateway_pid_unconfirmed")
        marker = self.profile / ".gateway-planned-stop.json"
        if marker.exists() or marker.is_symlink():
            raise ValueError("hermes_stop_marker_preexisting")
        # Match the official launchd_stop planned-stop protocol before
        # bootout.  Otherwise a deliberate SIGTERM is recorded as a crash.
        try:
            self._official(
                "from gateway.status import write_planned_stop_marker; "
                "raise SystemExit(0 if write_planned_stop_marker(int(sys.argv[3])) else 4)",
                str(running["pid"]),
            )
            result = subprocess.run(
                ["/bin/launchctl", "bootout", f"{self.domain}/ai.hermes.gateway"],
                capture_output=True,
                timeout=90,
                check=False,
                env=self._env(),
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise HermesStopAfterMarkerUnconfirmed(
                "hermes_gateway_stop_marker_requires_recovery"
            ) from exc
        if result.returncode != 0:
            raise HermesStopAfterMarkerUnconfirmed("hermes_gateway_stop_marker_requires_recovery")

    def planned_stop_marker_absent(self):
        """Absence is a guard, not proof of how the stop marker disappeared."""
        marker = self.profile / ".gateway-planned-stop.json"
        try:
            marker.lstat()
        except FileNotFoundError:
            return True
        return False

    def strict_gateway_absent(self):
        """Ask the installed Hermes lock probe without cleaning stale records."""
        for name in ("gateway.pid", "gateway.lock"):
            try:
                info = (self.profile / name).lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return False
        output = self._official(
            "from gateway.status import get_running_pid_identity_strict; "
            "from pathlib import Path; "
            "identity=get_running_pid_identity_strict(Path(sys.argv[2])/'gateway.pid'); "
            "sys.stdout.write('absent\\n' if identity is None else 'present\\n')"
        )
        return output == b"absent\n"

    def start_stopped_original(self, domain, *, plist_sha256, command_sha256):
        """Restore only the journal's original service; never pick a new domain."""
        if (
            self.domain is not None
            or domain not in (f"gui/{os.getuid()}", f"user/{os.getuid()}")
            or plist_sha256 != self.plist_digest.hex()
            or command_sha256 != self.command_digest.hex()
            or not self._same_installation()
            or not self.planned_stop_marker_absent()
            or not self.service_stopped()
            or not self.strict_gateway_absent()
        ):
            raise ValueError("hermes_gateway_restore_scope_unconfirmed")
        self.domain = domain
        try:
            self.start()
        finally:
            self.domain = None

    def restored_original_route(self, domain, endpoint, original_pid, original_start_time):
        """Verify a new live process in the same launchd job and receiver route."""
        if _loaded_service_domain(self.command, self.plist, self.profile) != domain:
            return False
        runtime = self.runtime()
        route = self.routing(endpoint)
        pid = runtime.get("pid")
        start = runtime.get("start_time")
        return (
            runtime.get("kind") == "hermes-gateway"
            and runtime.get("gateway_state") == "running"
            and runtime.get("hermes_home") == str(self.profile)
            and type(pid) is int
            and pid > 0
            and type(start) is int
            and start > 0
            and (pid, start) != (original_pid, original_start_time)
            and self.pid_identity(runtime)
            and isinstance(route, dict)
            and route.get("protocol") == "agent-mailbox.hermes-routing.v1"
            and route.get("host_kind") == "hermes"
            and route.get("pid") == pid
            and route.get("profile_home") == str(self.profile)
            and route.get("routing_home") == str(self.profile)
            and isinstance(route.get("instance_id"), str)
            and bool(route["instance_id"])
        )

    def start(self):
        if self.domain is None or not self._same_installation() or not self.service_stopped():
            raise ValueError("hermes_gateway_start_scope_unconfirmed")
        result = subprocess.run(
            ["/bin/launchctl", "bootstrap", self.domain, str(self.plist)],
            capture_output=True,
            timeout=30,
            check=False,
            env=self._env(),
        )
        if result.returncode != 0:
            raise ValueError("hermes_gateway_start_unconfirmed")
        result = subprocess.run(
            ["/bin/launchctl", "kickstart", f"{self.domain}/ai.hermes.gateway"],
            capture_output=True,
            timeout=30,
            check=False,
            env=self._env(),
        )
        if result.returncode != 0:
            raise ValueError("hermes_gateway_start_unconfirmed")
