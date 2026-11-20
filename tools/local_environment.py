from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid

from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "reporting"
RUN_BACKEND = APP_DIR / "run_backend.py"
ENV_FILE = APP_DIR / ".env"
ROOT_ENV_FILE = REPO_ROOT / ".env"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(APP_DIR))

from core.env_loader import load_env_file

load_env_file(ENV_FILE if ENV_FILE.is_file() else ROOT_ENV_FILE)

from reporting.backend.report_builder import (
    OUTPUT_PATH,
    ReportBuildError,
    rebuild_report,
    source_fingerprints,
    validate_payload,
)


class LocalEnvironmentError(RuntimeError):
    pass


def runtime_dir() -> Path:
    return Path(os.environ.get("REPORTING_RUNTIME_DIR", APP_DIR / "runtime")).resolve()


def server_state_path() -> Path:
    return runtime_dir() / "local-server.json"


def server_code_paths() -> list[Path]:
    """Return every Python module loaded by the long-running local process."""
    return [
        RUN_BACKEND,
        *sorted((APP_DIR / "backend").glob("*.py")),
        *sorted((REPO_ROOT / "core").glob("*.py")),
    ]


def server_code_fingerprint() -> str:
    """Identify backend and shared-core code loaded by the local process."""
    digest = hashlib.sha256()
    for path in server_code_paths():
        digest.update(str(path.relative_to(REPO_ROOT)).replace("\\", "/").encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def server_code_is_current() -> bool:
    state = _read_server_state()
    return bool(state.get("code_fingerprint")) and state.get("code_fingerprint") == server_code_fingerprint()


def server_configuration() -> tuple[str, int]:
    host = os.environ.get("REPORTING_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("REPORTING_PORT", "4174"))
    except ValueError as error:
        raise LocalEnvironmentError("REPORTING_PORT doit etre un nombre entier.") from error
    if not 1 <= port <= 65535:
        raise LocalEnvironmentError("REPORTING_PORT doit etre compris entre 1 et 65535.")
    return host, port


def health_url(host: str, port: int) -> str:
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else host
    if ":" in connect_host and not connect_host.startswith("["):
        connect_host = f"[{connect_host}]"
    return f"http://{connect_host}:{port}/api/health"


def health_status(host: str, port: int, *, timeout: float = 0.8) -> dict | None:
    try:
        with urlopen(health_url(host, port), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if response.status == 200 and payload.get("status") == "ok" else None
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return None


def is_healthy(
    host: str,
    port: int,
    *,
    timeout: float = 0.8,
    instance_token: str | None = None,
) -> bool:
    status = health_status(host, port, timeout=timeout)
    return bool(status) and (
        instance_token is None or status.get("instance") == instance_token
    )


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _powershell_output(command: str) -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        return ""
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _command_line(pid: int) -> str:
    if os.name == "nt":
        return _powershell_output(
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' "
            "-ErrorAction SilentlyContinue).CommandLine"
        )
    proc_path = Path("/proc") / str(pid) / "cmdline"
    try:
        return proc_path.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
    except OSError:
        return ""


def _process_executable(pid: int) -> str:
    if os.name == "nt":
        return _powershell_output(
            f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).Path"
        )
    proc_path = Path("/proc") / str(pid) / "exe"
    try:
        return str(proc_path.resolve())
    except OSError:
        return ""


def _is_owned_server_process(pid: int) -> bool:
    if not _process_is_running(pid):
        return False
    command_line = _command_line(pid).casefold()
    expected_script = str(RUN_BACKEND.resolve()).casefold()
    return expected_script in command_line


def _listener_pid(port: int) -> int | None:
    if os.name != "nt":
        return None
    output = _powershell_output(
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue "
        f"| Where-Object {{ $_.LocalPort -eq {int(port)} }} "
        "| Select-Object -First 1 -ExpandProperty OwningProcess"
    )
    try:
        return int(output.splitlines()[0]) if output else None
    except (ValueError, IndexError):
        return None


def _read_server_state() -> dict:
    path = server_state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_server_state(pid: int, host: str, port: int, instance_token: str) -> None:
    path = server_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "pid": pid,
        "host": host,
        "port": port,
        "repo_root": str(REPO_ROOT.resolve()),
        "instance": instance_token,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "code_fingerprint": server_code_fingerprint(),
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def owned_server_pid(port: int) -> int | None:
    state = _read_server_state()
    try:
        state_pid = int(state.get("pid", 0))
    except (TypeError, ValueError):
        state_pid = 0
    state_root = str(state.get("repo_root", ""))
    try:
        state_port = int(state.get("port", 0))
    except (TypeError, ValueError):
        state_port = 0
    state_token = str(state.get("instance", "")) or None
    state_host = str(state.get("host", "127.0.0.1"))
    executable_matches = (
        os.path.normcase(_process_executable(state_pid))
        == os.path.normcase(str(Path(sys.executable).resolve()))
    )
    if (
        state_root == str(REPO_ROOT.resolve())
        and state_port == port
        and _process_is_running(state_pid)
        and executable_matches
        and is_healthy(state_host, port, instance_token=state_token)
    ):
        return state_pid

    listener_pid = _listener_pid(port)
    if listener_pid and _is_owned_server_process(listener_pid):
        return listener_pid
    return None


def validate_published_report() -> dict:
    if not OUTPUT_PATH.is_file():
        raise LocalEnvironmentError(
            f"Donnees calculees absentes : {OUTPUT_PATH}. Lancez refresh_local."
        )
    try:
        payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        validate_payload(payload)
    except (OSError, json.JSONDecodeError, ReportBuildError) as error:
        raise LocalEnvironmentError(f"Publication locale invalide : {error}") from error
    return payload


def verify_sources() -> dict:
    try:
        fingerprints = source_fingerprints()
    except ReportBuildError as error:
        raise LocalEnvironmentError(str(error)) from error
    if len(fingerprints) != 11:
        raise LocalEnvironmentError(f"{len(fingerprints)} sources trouvees au lieu de 11.")
    empty = [source_id for source_id, state in fingerprints.items() if not state["size"]]
    if empty:
        raise LocalEnvironmentError(f"Sources vides : {', '.join(empty)}")
    return fingerprints


def refresh_local() -> dict:
    before = verify_sources()
    try:
        payload = rebuild_report()
    except ReportBuildError as error:
        raise LocalEnvironmentError(str(error)) from error
    after = verify_sources()
    if after != before:
        raise LocalEnvironmentError(
            "Une source a change pendant le recalcul ; relancez refresh_local."
        )
    validate_published_report()
    return payload


def start_local() -> int:
    validate_published_report()
    host, port = server_configuration()
    if is_healthy(host, port) and server_code_is_current():
        print(f"Application deja disponible sur {health_url(host, port).removesuffix('/api/health')}/")
        return owned_server_pid(port) or 0
    if is_healthy(host, port):
        print("Le code du backend a change : redemarrage du serveur local.")
        stop_local()

    listener_pid = _listener_pid(port)
    if listener_pid:
        raise LocalEnvironmentError(
            f"Le port {port} est deja utilise par un autre processus (PID {listener_pid})."
        )

    destination = runtime_dir()
    destination.mkdir(parents=True, exist_ok=True)
    stdout_path = destination / "local-server.out.log"
    stderr_path = destination / "local-server.err.log"
    creation_flags = 0
    popen_options: dict = {}
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        popen_options["start_new_session"] = True

    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        instance_token = uuid.uuid4().hex
        server_environment = os.environ.copy()
        server_environment["REPORTING_INSTANCE_TOKEN"] = instance_token
        process = subprocess.Popen(
            [
                sys.executable,
                str(RUN_BACKEND.resolve()),
                "--host",
                host,
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            creationflags=creation_flags,
            env=server_environment,
            **popen_options,
        )
    _write_server_state(process.pid, host, port, instance_token)

    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if is_healthy(host, port, instance_token=instance_token):
            print(f"Application disponible sur {health_url(host, port).removesuffix('/api/health')}/")
            return process.pid
        time.sleep(0.2)

    if process.poll() is None:
        process.terminate()
    server_state_path().unlink(missing_ok=True)
    raise LocalEnvironmentError(
        f"Le serveur n'a pas demarre. Consultez {stderr_path}."
    )


def stop_local() -> bool:
    host, port = server_configuration()
    pid = owned_server_pid(port)
    if not pid:
        if is_healthy(host, port):
            raise LocalEnvironmentError(
                "Un serveur repond sur le port configure mais son appartenance au projet n'est pas verifiable."
            )
        server_state_path().unlink(missing_ok=True)
        return False

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and _process_is_running(pid):
        time.sleep(0.2)
    if _process_is_running(pid):
        raise LocalEnvironmentError(f"Le serveur local (PID {pid}) ne s'est pas arrete.")
    server_state_path().unlink(missing_ok=True)
    return True


def run_tests() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(APP_DIR / "tests"),
            "-p",
            "test_*.py",
        ],
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode:
        raise LocalEnvironmentError("Les tests ont echoue ; deploiement annule.")


def deploy_local() -> dict:
    print("[1/4] Verification des 11 sources locales")
    verify_sources()
    print("[2/4] Tests de non-regression")
    run_tests()
    print("[3/4] Reconstruction unique des donnees calculees")
    payload = refresh_local()
    print("[4/4] Redemarrage du serveur local")
    stop_local()
    start_local()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gestion fiable de l'environnement local")
    parser.add_argument("action", choices=("start", "refresh", "deploy"))
    args = parser.parse_args(argv)

    try:
        if args.action == "start":
            start_local()
        elif args.action == "refresh":
            payload = refresh_local()
            print(
                "Donnees reconstruites : "
                f"{payload['meta']['periodLabel']} - {payload['summary']['projects']} projets."
            )
        else:
            payload = deploy_local()
            print(
                "Deploiement local termine : "
                f"donnees du {payload['meta']['dataThrough']} publiees."
            )
        return 0
    except (LocalEnvironmentError, OSError, subprocess.SubprocessError) as error:
        print(f"ERREUR : {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
