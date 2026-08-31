"""Command-line copilot.

    python -m copilot.cli ask "why did latency spike at 19:40?"
    python -m copilot.cli replay 2026-08-30T19:00:00Z 2026-08-30T20:00:00Z
    python -m copilot.cli chat            # interactive, shares one receipt chain
    python -m copilot.cli tools           # list the read-only tools, no model call

Every path prints what it cost and where the receipt went. `--export DIR` writes
each answer's receipt as a standalone JSON file, which is what
`python -m copilot.verify` then reads.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from day2_mcp.server import COPILOT_SCOPES, CopilotConfig, ToolRegistry

from copilot.receipts import load_or_create_key, write_receipt
from copilot.replay import Replay, run_replay
from copilot.runtime import CopilotSession, Turn
from day2_agents.audit import AuditLogger
from day2_agents.scopes import PermissionSet

# ANSI, used sparingly. Disabled when stdout is not a terminal so piping to a
# file or a log does not fill it with escape codes.
_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def dim(t: str) -> str:
    return _c(t, "2")


def bold(t: str) -> str:
    return _c(t, "1")


def green(t: str) -> str:
    return _c(t, "32")


def red(t: str) -> str:
    return _c(t, "31")


def yellow(t: str) -> str:
    return _c(t, "33")


def build_session(args: argparse.Namespace) -> CopilotSession:
    repo_root = Path(args.repo_root).resolve()
    audit = AuditLogger(
        agent="observability-copilot",
        trigger=args.command,
        path=args.audit_log,
    )
    scopes = PermissionSet.declare("observability-copilot", COPILOT_SCOPES)
    registry = ToolRegistry(
        config=CopilotConfig(
            prometheus_url=args.prometheus,
            loki_url=args.loki,
            repo_root=repo_root,
        ),
        scopes=scopes,
        audit=audit,
    )
    return CopilotSession(
        registry=registry,
        audit=audit,
        scopes=scopes,
        signing_key=load_or_create_key(),
        budget_usd=args.budget,
    )


def print_turn(turn: Turn, session: CopilotSession, export: Path | None) -> None:
    print()
    print(turn.answer)
    print()

    if turn.evidence:
        print(dim("evidence"))
        for entry in turn.evidence:
            mark = red("!") if entry.is_error else " "
            detail = entry.error if entry.is_error else (entry.citation_id or "")
            args_preview = str(entry.arguments)[:70]
            print(dim(f"  {mark} {entry.tool:<18} {detail}"))
            print(dim(f"      {args_preview}"))

    if turn.supported:
        print(green(f"  supported — {len(turn.citations)} citation(s)"))
    else:
        print(red(f"  UNSUPPORTED — {turn.unsupported_reason}"))

    print(
        dim(
            f"  ${turn.cost_usd:.4f} this answer · ${session.total_cost_usd:.4f} "
            f"of ${session.budget_usd:.2f} session budget · {turn.elapsed_ms}ms"
        )
    )

    if export and turn.receipt:
        target = (
            Path(export) / f"receipt-{turn.receipt['sequence']:03d}-"
            f"{turn.receipt['receipt_id'][:8]}.json"
        )
        write_receipt(turn.receipt, target)
        print(dim(f"  receipt -> {target}"))


def print_replay(replay: Replay, session: CopilotSession, export: Path | None) -> None:
    window = replay.window
    print()
    print(bold(f"Replay  {window.start.isoformat()} → {window.end.isoformat()}"))
    print()
    if replay.summary:
        print(replay.summary)
        print()

    if not replay.timeline:
        print(yellow("  (no timeline entries — see the summary above)"))
    for entry in replay.timeline:
        icon = {
            "metrics": "▲",
            "logs": "≡",
            "alerts": "!",
            "deploy": "⎇",
        }.get(entry.source, "·")
        stamp = entry.timestamp[11:19] if len(entry.timestamp) > 19 else entry.timestamp
        print(f"  {dim(stamp)}  {icon} {bold(entry.headline)}")
        print(f"            {entry.detail}")
        print(dim(f"            [{entry.citation_id}]"))

    if replay.conclusion:
        print()
        print(bold("Conclusion"))
        print(f"  {replay.conclusion}")

    if replay.dropped:
        print()
        print(red(f"  {len(replay.dropped)} entry/entries dropped for citing evidence"))
        print(red("  this replay did not produce:"))
        for item in replay.dropped:
            print(red(f"    - {item['headline']} ({item['citation_id']})"))

    if replay.turn:
        print_turn_footer(replay.turn, session, export)


def print_turn_footer(turn: Turn, session: CopilotSession, export: Path | None) -> None:
    print()
    if turn.supported:
        print(green(f"  supported — {len(turn.citations)} citation(s)"))
    else:
        print(red(f"  UNSUPPORTED — {turn.unsupported_reason}"))
    print(
        dim(
            f"  ${turn.cost_usd:.4f} · session ${session.total_cost_usd:.4f} "
            f"of ${session.budget_usd:.2f} · {turn.elapsed_ms}ms"
        )
    )
    if export and turn.receipt:
        target = Path(export) / f"receipt-{turn.receipt['sequence']:03d}-replay.json"
        write_receipt(turn.receipt, target)
        print(dim(f"  receipt -> {target}"))


def cmd_tools(args: argparse.Namespace) -> int:
    """List the tool surface without spending anything."""
    session = build_session(args)
    print(bold("read-only tools"))
    for tool in session.registry.schemas():
        params = ", ".join(tool["input_schema"]["properties"])
        print(f"  {tool['name']:<18} ({params})")
    print()
    print(bold("declared scopes"), dim("— every one a read"))
    for scope in session.registry.declared_scopes():
        print(f"  {scope}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    session = build_session(args)
    export = Path(args.export) if args.export else None
    turn = session.ask(args.question)
    print_turn(turn, session, export)
    return 0 if turn.supported else 2


def cmd_replay(args: argparse.Namespace) -> int:
    session = build_session(args)
    export = Path(args.export) if args.export else None
    replay = run_replay(session, args.start, args.end)
    print_replay(replay, session, export)
    return 0 if (replay.turn and replay.turn.supported) else 2


def cmd_chat(args: argparse.Namespace) -> int:
    session = build_session(args)
    export = Path(args.export) if args.export else None
    print(bold("Observability Copilot") + dim("  — ctrl-D or 'exit' to leave"))
    print(
        dim(
            f"  budget ${session.budget_usd:.2f} · "
            f"{len(session.registry.tool_names())} read-only tools"
        )
    )

    while True:
        try:
            question = input(bold("\n> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break
        if question.lower().startswith("replay "):
            parts = question.split()
            if len(parts) != 3:
                print(red("  usage: replay <start> <end>"))
                continue
            print_replay(run_replay(session, parts[1], parts[2]), session, export)
            continue
        try:
            turn = session.ask(question)
        except Exception as exc:
            print(red(f"  {type(exc).__name__}: {exc}"))
            continue
        print_turn(turn, session, export)

    print()
    print(
        dim(
            f"session total ${session.total_cost_usd:.4f} across "
            f"{len(session.turns)} answer(s); {len(session.chain.receipts)} receipts"
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.cli",
        description="Ask the Observability Copilot about the running system.",
    )
    parser.add_argument(
        "--prometheus",
        default=os.environ.get("DAY2_PROMETHEUS_URL", "http://localhost:9090"),
    )
    parser.add_argument(
        "--loki", default=os.environ.get("DAY2_LOKI_URL", "http://localhost:3100")
    )
    parser.add_argument("--repo-root", default=os.environ.get("DAY2_REPO_ROOT", "."))
    parser.add_argument(
        "--audit-log", default=os.environ.get("DAY2_AUDIT_LOG", "copilot-audit.jsonl")
    )
    parser.add_argument("--export", default=None, help="directory to write receipts into")
    parser.add_argument(
        "--budget",
        type=float,
        default=float(os.environ.get("DAY2_COPILOT_BUDGET", "0.50")),
        help="hard session spend cap in USD (default 0.50)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="answer one question")
    ask.add_argument("question")
    ask.set_defaults(func=cmd_ask)

    replay = sub.add_parser("replay", help="reconstruct a time window as a timeline")
    replay.add_argument("start", help="RFC3339, e.g. 2026-08-30T19:00:00Z")
    replay.add_argument("end")
    replay.set_defaults(func=cmd_replay)

    chat = sub.add_parser("chat", help="interactive session sharing one receipt chain")
    chat.set_defaults(func=cmd_chat)

    tools = sub.add_parser("tools", help="list the tool surface (no model call)")
    tools.set_defaults(func=cmd_tools)

    args = parser.parse_args(argv)
    if args.export:
        Path(args.export).mkdir(parents=True, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
