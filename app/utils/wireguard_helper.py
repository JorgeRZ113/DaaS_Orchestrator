import argparse
import os
import subprocess
import sys
from pathlib import Path


def _find_windows_wireguard_exe() -> str:
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / "wireguard.exe"
        if candidate.exists():
            return str(candidate)

    candidates = [
        r"C:\Program Files\WireGuard\wireguard.exe",
        r"C:\Program Files (x86)\WireGuard\wireguard.exe",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate

    raise FileNotFoundError("wireguard.exe not found")


def _build_command(action: str, tn_id: str, conf_path: str) -> list[str]:
    if os.name == "nt":
        exe = _find_windows_wireguard_exe()
        if action == "up":
            return [exe, "/installtunnelservice", conf_path]
        if action == "down":
            return [exe, "/uninstalltunnelservice", tn_id]
        raise ValueError(f"Unsupported action: {action}")

    if action == "up":
        return ["wg-quick", "up", conf_path]
    if action == "down":
        return ["wg-quick", "down", tn_id]
    raise ValueError(f"Unsupported action: {action}")


def _linux_interface_exists(tn_id: str) -> bool:
    result = subprocess.run(["ip", "link", "show", "dev", tn_id], capture_output=True, text=True)
    return result.returncode == 0


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["up", "down"])
    parser.add_argument("--tn-id", required=True)
    parser.add_argument("--conf-path", required=True)
    args = parser.parse_args()

    if args.action == "up" and os.name != "nt" and _linux_interface_exists(args.tn_id):
        subprocess.run(["wg-quick", "down", args.tn_id], capture_output=True, text=True)

    if args.action == "up" and os.name == "nt":
        subprocess.run(
            _build_command("down", args.tn_id, args.conf_path), capture_output=True, text=True
        )

    command = _build_command(args.action, args.tn_id, args.conf_path)
    result = _run_command(command)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
