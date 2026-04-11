import logging
import os
import subprocess
import sys
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


class WireGuardError(RuntimeError):
    """Raised when WireGuard command execution fails."""


def _artifacts_dir_for_execution(execution_id: str) -> Path:
    base = Path(settings.artifacts_dir)
    if not base.is_absolute():
        base = Path.cwd() / base
    execution_dir = base / execution_id
    execution_dir.mkdir(parents=True, exist_ok=True)
    return execution_dir


def write_tunnel_conf(execution_id: str, tn_id: str, config_text: str) -> str:
    if not config_text or not config_text.strip():
        raise WireGuardError("WireGuard config is empty in TNLCM report")

    conf_path = _artifacts_dir_for_execution(execution_id) / f"{tn_id}.conf"
    conf_path.write_text(config_text.strip() + "\n", encoding="utf-8")
    logger.info("WireGuard config written: %s", conf_path)
    return str(conf_path)


def _helper_command(action: str, tn_id: str, conf_path: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "app.utils.wireguard_helper",
        action,
        "--tn-id",
        tn_id,
        "--conf-path",
        conf_path,
    ]


def _hide_window_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {
        "startupinfo": startupinfo,
        "creationflags": creationflags,
    }


def _run_helper(action: str, tn_id: str, conf_path: str) -> subprocess.CompletedProcess[str]:
    command = _helper_command(action, tn_id, conf_path)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        **_hide_window_kwargs(),
    )


def _raise_helper_error(action: str, tn_id: str, result: subprocess.CompletedProcess[str]) -> None:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    details = stderr or stdout or f"exit_code={result.returncode}"
    raise WireGuardError(f"WireGuard {action} failed for {tn_id}: {details}")


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_helper_elevated(action: str, tn_id: str, conf_path: str) -> None:
    if os.name != "nt":
        raise WireGuardError("Elevation helper is only available on Windows")

    command = _helper_command(action, tn_id, conf_path)
    arg_list = ",".join(_ps_quote(item) for item in command[1:])
    ps_script = (
        f"$p = Start-Process -FilePath {_ps_quote(command[0])} "
        f"-ArgumentList @({arg_list}) -Verb RunAs -WindowStyle Hidden -PassThru -Wait; "
        f"exit $p.ExitCode"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
        capture_output=True,
        text=True,
        **_hide_window_kwargs(),
    )
    if result.returncode != 0:
        _raise_helper_error(action, tn_id, result)


def _needs_windows_elevation(result: subprocess.CompletedProcess[str]) -> bool:
    stderr = (result.stderr or "").lower()
    stdout = (result.stdout or "").lower()
    details = f"{stderr} {stdout}"
    return result.returncode != 0 and ("access is denied" in details or "privilege" in details)


def _execute(action: str, tn_id: str, conf_path: str) -> None:
    result = _run_helper(action, tn_id, conf_path)
    if result.returncode == 0:
        logger.info("WireGuard %s completed for %s", action, tn_id)
        return

    if os.name == "nt" and _needs_windows_elevation(result):
        logger.info("WireGuard %s requires elevation for %s; retrying elevated helper", action, tn_id)
        _run_helper_elevated(action, tn_id, conf_path)
        return

    _raise_helper_error(action, tn_id, result)


def up_tunnel(tn_id: str, conf_path: str) -> None:
    _execute("up", tn_id, conf_path)


def down_tunnel(tn_id: str, conf_path: str | None = None) -> None:
    if not conf_path:
        conf_path = str(_artifacts_dir_for_execution(tn_id) / f"{tn_id}.conf")
    _execute("down", tn_id, conf_path)


