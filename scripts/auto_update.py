#!/usr/bin/env python3
"""Monitor the remote main branch and restart ``main.py`` when updates arrive.

This script is intended to run continuously inside the container that hosts the
application. It periodically compares the local ``main`` branch with the remote
branch (``origin/main`` by default). When it detects that the remote branch has
advanced, it resets the working tree to match the remote, then restarts the
configured application command (``python3 main.py`` by default).

Authentication
-------------
If the GitHub repository requires authentication, export a ``GITHUB_PAT``
environment variable before starting this watcher. The token will automatically
be injected into the ``origin`` URL for ``git`` commands while the script is
running, so the repository configuration itself does not need to store the
credential.

Example usage::

    export GITHUB_PAT="<your-token>"
    python3 scripts/auto_update.py --interval 300

"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_git_command(args: list[str], env: Optional[dict[str, str]] = None) -> subprocess.CompletedProcess:
    """Run a git command within the repository root and return the result."""
    git_env = os.environ.copy()
    if env:
        git_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
        env=git_env,
    )


def _prepare_git_env() -> dict[str, str]:
    """Build an environment dictionary that injects ``GITHUB_PAT`` if provided."""
    env: dict[str, str] = {
        "GIT_TERMINAL_PROMPT": "0",
    }
    return env


def _rewrite_remote_url(token: str) -> None:
    """Temporarily rewrite the origin URL to include the provided token."""
    origin_url = _run_git_command(["remote", "get-url", "origin"]).stdout.strip()
    if token in origin_url:
        # Token already present, nothing to do.
        return

    if not origin_url.startswith("https://"):
        raise RuntimeError(
            "Token-based authentication requires an HTTPS remote URL. Current"
            f" origin URL is '{origin_url}'."
        )

    token_url = origin_url.replace("https://", f"https://{token}:@", 1)
    _run_git_command(["remote", "set-url", "origin", token_url])


def _restore_remote_url(original_url: str) -> None:
    """Restore the ``origin`` URL after temporarily injecting credentials."""
    _run_git_command(["remote", "set-url", "origin", original_url])


def _with_token_env() -> dict[str, str]:
    """Prepare git environment, temporarily adjusting the remote if needed."""
    env = _prepare_git_env()
    token = os.environ.get("GITHUB_PAT")
    if not token:
        return env

    original_url = _run_git_command(["remote", "get-url", "origin"]).stdout.strip()
    try:
        _rewrite_remote_url(token)
    except Exception:
        # Restore the original URL on failure so subsequent calls behave.
        _restore_remote_url(original_url)
        raise

    env["_ORIGINAL_ORIGIN_URL"] = original_url
    return env


def _cleanup_token_env(env: dict[str, str]) -> None:
    """Restore the origin URL if it was rewritten for authentication."""
    original = env.get("_ORIGINAL_ORIGIN_URL")
    if original:
        _restore_remote_url(original)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--branch",
        default="main",
        help="Branch to track for updates (default: main)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds to wait between update checks (default: 300)",
    )
    parser.add_argument(
        "--command",
        default="python3 main.py",
        help="Command used to run the application (default: python3 main.py)",
    )
    return parser.parse_args()


def start_process(command: str) -> subprocess.Popen:
    """Start the application command and return the process handle."""
    return subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        shell=True,
        preexec_fn=os.setsid,  # Allow sending signals to the entire process group.
    )


def stop_process(process: Optional[subprocess.Popen]) -> None:
    """Terminate the running process if it exists."""
    if not process or process.poll() is not None:
        return

    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def check_for_updates(branch: str, env: dict[str, str]) -> bool:
    """Fetch remote updates and return True when the local branch is behind."""
    _run_git_command(["fetch", "origin", branch], env=env)
    local_ref = _run_git_command(["rev-parse", branch], env=env).stdout.strip()
    remote_ref = _run_git_command(["rev-parse", f"origin/{branch}"], env=env).stdout.strip()
    return local_ref != remote_ref


def sync_branch(branch: str, env: dict[str, str]) -> None:
    """Reset the local branch to match the remote branch exactly."""
    _run_git_command(["reset", "--hard", f"origin/{branch}"], env=env)
    _run_git_command(["clean", "-fd"], env=env)


def ensure_branch_exists(branch: str, env: dict[str, str]) -> None:
    """Make sure the target branch is present locally before monitoring."""
    try:
        _run_git_command(["rev-parse", branch], env=env)
    except subprocess.CalledProcessError:
        _run_git_command(["fetch", "origin", branch], env=env)
        _run_git_command(["checkout", "-B", branch, f"origin/{branch}"], env=env)


def main() -> int:
    args = parse_args()
    env = _with_token_env()
    process: Optional[subprocess.Popen] = None

    def _shutdown(signum: int, _: Optional[object]) -> None:
        stop_process(process)
        _cleanup_token_env(env)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        ensure_branch_exists(args.branch, env)
        process = start_process(args.command)
        while True:
            time.sleep(args.interval)
            if check_for_updates(args.branch, env):
                sync_branch(args.branch, env)
                stop_process(process)
                process = start_process(args.command)
    finally:
        stop_process(process)
        _cleanup_token_env(env)

    return 0


if __name__ == "__main__":
    sys.exit(main())
