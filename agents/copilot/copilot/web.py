"""The copilot's web surface: chat, an evidence sidebar, and replay.

    python -m copilot.web --repo-root /path/to/repo

Deliberately built on `http.server` from the standard library. This is a demo
surface for one operator on localhost, and a framework would add dependencies,
a build step and a install-time failure mode to a page that is ultimately one
HTML file and two JSON endpoints. `ThreadingHTTPServer` is enough because a
question is one blocking call and the evidence sidebar needs no streaming.

The one thing worth saying about the design: the evidence sidebar is not
decoration. An answer here is only as good as what it read, so the tool calls
are given equal visual weight to the prose — same panel width, always visible,
never collapsed by default. Clicking a citation in the answer scrolls to and
highlights the evidence that supports it, so "check the source" is one click
rather than a mental cross-reference.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from day2_mcp.server import COPILOT_SCOPES, CopilotConfig, ToolRegistry

from copilot.receipts import load_or_create_key
from copilot.replay import run_replay
from copilot.runtime import CopilotSession, Turn
from day2_agents.audit import AuditLogger
from day2_agents.scopes import PermissionSet

_LOCK = threading.Lock()


def _turn_payload(turn: Turn, session: CopilotSession) -> dict[str, Any]:
    return {
        "question": turn.question,
        "answer": turn.answer,
        "citations": turn.citations,
        "supported": turn.supported,
        "unsupported_reason": turn.unsupported_reason,
        "cost_usd": round(turn.cost_usd, 6),
        "elapsed_ms": turn.elapsed_ms,
        "model_calls": turn.model_calls,
        "sequence": turn.receipt["sequence"] if turn.receipt else None,
        "receipt_id": turn.receipt["receipt_id"] if turn.receipt else None,
        "evidence": [
            {
                "index": e.index,
                "tool": e.tool,
                "arguments": e.arguments,
                "citation_id": e.citation_id,
                "provenance": e.provenance,
                "is_error": e.is_error,
                "error": e.error,
                "elapsed_ms": e.elapsed_ms,
                "redactions": e.redactions,
                "truncated": e.truncated,
                "result_digest": e.result_digest,
            }
            for e in turn.evidence
        ],
        "session": {
            "total_usd": round(session.total_cost_usd, 6),
            "budget_usd": session.budget_usd,
            "remaining_usd": round(session.budget_remaining, 6),
            "turns": len(session.turns),
        },
    }


class Handler(BaseHTTPRequestHandler):
    session: CopilotSession = None  # type: ignore[assignment]
    server_version = "day2-copilot"

    def log_message(self, fmt: str, *args: Any) -> None:
        # The default logs every request to stderr, which would clutter a
        # screen recording running beside the browser.
        return

    # -- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, load_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path == "/api/state":
            self._json(
                200,
                {
                    "tools": self.session.registry.tool_names(),
                    "scopes": self.session.registry.declared_scopes(),
                    "budget_usd": self.session.budget_usd,
                    "total_usd": round(self.session.total_cost_usd, 6),
                    "turns": len(self.session.turns),
                    "session_id": self.session.chain.session_id,
                    "key_fingerprint": self.session.chain.key.fingerprint,
                },
            )
            return
        if self.path.startswith("/api/receipt/"):
            try:
                sequence = int(self.path.rsplit("/", 1)[1])
                receipt = self.session.chain.receipts[sequence]
            except (ValueError, IndexError):
                self._json(404, {"error": "no such receipt"})
                return
            body = (json.dumps(receipt, indent=2, ensure_ascii=False) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="receipt-{sequence:03d}.json"',
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/api/ask":
            payload = self._read_json()
            question = (payload.get("question") or "").strip()
            if not question:
                self._json(400, {"error": "a question is required"})
                return
            with _LOCK:
                try:
                    turn = self.session.ask(question)
                except Exception as exc:
                    self._json(200, {"error": f"{type(exc).__name__}: {exc}"})
                    return
            self._json(200, _turn_payload(turn, self.session))
            return

        if self.path == "/api/replay":
            payload = self._read_json()
            start, end = payload.get("start", ""), payload.get("end", "")
            with _LOCK:
                try:
                    replay = run_replay(self.session, start, end)
                except Exception as exc:
                    self._json(200, {"error": f"{type(exc).__name__}: {exc}"})
                    return
            body = replay.as_dict()
            body["turn"] = (
                _turn_payload(replay.turn, self.session) if replay.turn else None
            )
            self._json(200, body)
            return

        self._json(404, {"error": "not found"})


# The page lives in `static/index.html` rather than in a Python string. It is
# HTML, CSS and JavaScript — keeping it in its own file means it gets the right
# editor tooling and is not measured against Python's line-length rules, and it
# can be edited without touching the server.
_STATIC = Path(__file__).resolve().parent / "static"


def load_page() -> str:
    """Read the single-page UI. Read per request so an edit shows on reload."""
    return (_STATIC / "index.html").read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m copilot.web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--repo-root", default=os.environ.get("DAY2_REPO_ROOT", "."))
    parser.add_argument(
        "--prometheus",
        default=os.environ.get("DAY2_PROMETHEUS_URL", "http://localhost:9090"),
    )
    parser.add_argument(
        "--loki", default=os.environ.get("DAY2_LOKI_URL", "http://localhost:3100")
    )
    parser.add_argument(
        "--audit-log", default=os.environ.get("DAY2_AUDIT_LOG", "copilot-audit.jsonl")
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=float(os.environ.get("DAY2_COPILOT_BUDGET", "0.50")),
    )
    parser.add_argument("--open", action="store_true", help="open a browser")
    args = parser.parse_args(argv)

    audit = AuditLogger("observability-copilot", "web", path=args.audit_log)
    scopes = PermissionSet.declare("observability-copilot", COPILOT_SCOPES)
    registry = ToolRegistry(
        CopilotConfig(
            prometheus_url=args.prometheus,
            loki_url=args.loki,
            repo_root=Path(args.repo_root).resolve(),
        ),
        scopes,
        audit,
    )
    Handler.session = CopilotSession(
        registry=registry,
        audit=audit,
        scopes=scopes,
        signing_key=load_or_create_key(),
        budget_usd=args.budget,
    )

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Observability Copilot on {url}")
    print(f"  budget ${args.budget:.2f} · {len(registry.tool_names())} read-only tools")
    print(f"  signing key {Handler.session.chain.key.fingerprint}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
