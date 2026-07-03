"""Local GitHub-webhook listener that triggers a Hooky run for forwarded events.

Pairs with `gh webhook forward` (or any relay that POSTs GitHub webhook
deliveries to a local URL): a developer runs `hooky listen` in a workspace,
points the relay at it, and gets the same trigger conditions, mode, and
run_key/proposal derivation as .github/workflows/hooky.yml, without pushing
anything or waiting on a hosted runner. Unlike the workflow, this command
never touches git remotes; it only runs the loop locally.

Mode determines which subcommand runs: "implement" and "light-implement" both
run `hooky run` (light-implement adds --light); "review" runs `hooky
review`; "refine" starts nothing at all - GitHub already persisted the
comment, and it's only meant to be folded into a future run's proposal.

Each triggered run is executed as a subprocess, not called in-process: role
timeouts in runtime/models.py use SIGALRM, which only works on a process's
main thread, and this handler runs in an HTTP server thread. Shelling out
also matches how .github/workflows/hooky.yml itself invokes the CLI, and
avoids mutating this process's environment (HOOKY_RUN_KEY) from a background
thread.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated

import typer

from hooky.cli.app import app
from hooky.cli.paths import DEFAULT_LAST_RUN_PATH, workspace_from_ctx
from hooky.shared.github_event import derive_run_from_event, determine_mode

_run_lock = threading.Lock()

_MODE_SUBCOMMAND = {
    "implement": "run",
    "light-implement": "run",
    "review": "review",
}


def verify_signature(secret: str, body: bytes, header_value: str) -> bool:
    if not header_value.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value[len("sha256=") :])


def hooky_executable() -> str:
    return shutil.which("hooky") or sys.argv[0]


def run_triggered_loop(workspace: Path, mode: str, run_key: str, proposal: str, executor: str | None, last_run_path: Path) -> None:
    subcommand = _MODE_SUBCOMMAND.get(mode)
    if subcommand is None:
        # "refine": accumulate context only, no run at all. GitHub already
        # persisted the comment; there's nothing more to do locally.
        typer.echo(f"[hooky listen] noted refine-only comment for run_key={run_key}; no run started")
        return
    typer.echo(f"[hooky listen] queued mode={mode} run_key={run_key}; waiting for the local executor lock")
    with _run_lock:
        typer.echo(f"[hooky listen] starting mode={mode} run_key={run_key}")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(proposal)
            proposal_path = Path(handle.name)
        command = [
            hooky_executable(),
            "-C",
            str(workspace),
            subcommand,
            "--run-key",
            run_key,
            "--proposal-file",
            str(proposal_path),
            "--last-run-path",
            str(last_run_path),
        ]
        if mode == "light-implement":
            command.append("--light")
        if executor:
            command.extend(["--executor", executor])
        try:
            completed = subprocess.run(command, check=False)
            outcome = "passed" if completed.returncode == 0 else f"exit_code={completed.returncode}"
            typer.echo(f"[hooky listen] finished mode={mode} run_key={run_key}: {outcome}")
        except OSError as exc:
            typer.echo(f"[hooky listen] mode={mode} run_key={run_key} failed to launch: {exc}")
        finally:
            proposal_path.unlink(missing_ok=True)


def make_handler(workspace: Path, executor: str | None, secret: str | None, last_run_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, log_format: str, *args: object) -> None:
            typer.echo(f"[hooky listen] {self.address_string()} {log_format % args}")

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/", "/healthz"}:
                self.respond(200, b"ok")
                return
            self.respond(404, b"not found")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if secret:
                signature = self.headers.get("X-Hub-Signature-256") or ""
                if not verify_signature(secret, body, signature):
                    self.respond(401, b"invalid signature")
                    return
            event_name = self.headers.get("X-GitHub-Event") or ""
            try:
                event = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self.respond(400, b"invalid JSON payload")
                return
            mode = determine_mode(event, event_name) if isinstance(event, dict) and event_name else ""
            if not mode:
                self.respond(202, b"ignored: event does not match a trigger condition")
                return
            run_id = uuid.uuid4().hex[:12]
            run_key, proposal = derive_run_from_event(event, event_name, run_id)
            self.respond(202, f"accepted: mode={mode} run_key={run_key}".encode())
            threading.Thread(
                target=run_triggered_loop,
                args=(workspace, mode, run_key, proposal, executor, last_run_path),
                daemon=True,
            ).start()

        def respond(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@app.command()
def listen(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Address to bind the local webhook listener to.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind the local webhook listener to.")] = 8787,
    executor: Annotated[str | None, typer.Option(help="Role executor: native, shell, codex, or claude. Defaults to HOOKY_EXECUTOR or native.")] = None,
    secret: Annotated[str | None, typer.Option(help="Verify X-Hub-Signature-256 against this shared secret. Defaults to HOOKY_WEBHOOK_SECRET.")] = None,
    last_run_path: Annotated[Path, typer.Option(help="Path used by `hooky watch`/`status` to find the latest triggered run.")] = DEFAULT_LAST_RUN_PATH,
) -> None:
    """Listen for GitHub webhook deliveries and run the loop locally, one event at a time.

    Trigger conditions, mode, and run_key/proposal derivation match
    .github/workflows/hooky.yml: a `hooky:run` label on an issue (or
    `workflow_dispatch`) runs the full loop; `/hooky run`/`/hooky go` on an
    issue also runs the full loop. A `hooky:run` label on a PR, or a `/hooky
    review` comment on a PR, runs a read-only review. `/hooky run`/`/hooky
    go` on a PR runs a light, unnegotiated implementation. Any other `/hooky
    <text>` comment, on an issue or a PR, is refine-only and starts nothing -
    it's folded into the proposal the next time a run actually fires. Runs
    are serialized against this workspace's working tree.
    """
    workspace = workspace_from_ctx(ctx)
    resolved_secret = secret or os.environ.get("HOOKY_WEBHOOK_SECRET")
    if not resolved_secret:
        typer.echo("warning: no webhook secret configured (--secret or HOOKY_WEBHOOK_SECRET); any local process that can reach this port can trigger a run", err=True)
    handler = make_handler(workspace, executor, resolved_secret, last_run_path)
    server = ThreadingHTTPServer((host, port), handler)
    typer.echo(f"workspace: {workspace}")
    typer.echo(f"listening: http://{host}:{port}")
    typer.echo(f"forward events here, e.g.: gh webhook forward --repo <owner>/<repo> --events issues,issue_comment,pull_request --url http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
