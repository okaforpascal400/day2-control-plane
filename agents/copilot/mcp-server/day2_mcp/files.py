"""Repository file reads: dashboards and runbooks, path-jailed.

`get_dashboard` and `read_runbook` both read files out of the working tree, and
both are constrained the same way, because a tool that takes a path from a
model is the single most reliably exploited shape in this whole server. The
model does not need to be adversarial for this to matter — a plausible-looking
`../../.env` is exactly the kind of path an LLM produces when it is trying to
be helpful about configuration.

The jail has three layers, and each catches something the others do not:

1. **Resolve, then verify containment.** `Path.resolve()` collapses `..` and
   follows symlinks, and the resolved path must be inside the allowed root.
   Checking *before* resolution is the classic mistake: `docs/../../.env`
   passes a naive `startswith("docs/")` check and lands outside.
2. **An extension allowlist.** Even inside the root, only the file types these
   tools exist to serve are readable (`.md` for runbooks, `.json` for
   dashboards). A `.env` that someone drops into `docs/` is still unreadable.
3. **A denylist of names that must never be served**, applied last, regardless
   of location or extension. This is belt-and-braces against a future root
   being widened by someone who did not read layers 1 and 2.

Symlinks deserve a note. `resolve()` follows them, so a symlink inside `docs/`
pointing at `/etc/shadow` resolves to `/etc/shadow`, lands outside the root, and
is refused by layer 1 — which is the behaviour we want, and the reason
containment is checked on the *resolved* path rather than the requested one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from day2_mcp.limits import MAX_FILE_BYTES, Truncation, enforce_result_bytes
from day2_mcp.provenance import Provenance

# Never served, wherever they are and whatever they are called.
FORBIDDEN_NAMES: tuple[str, ...] = (
    ".env",
    "terraform.tfstate",
    "id_rsa",
    "id_ed25519",
)
FORBIDDEN_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".tfvars",
    ".tfstate",
    ".p12",
    ".pfx",
    ".jks",
)
FORBIDDEN_PREFIXES: tuple[str, ...] = ("kubeconfig-",)


class PathRefused(RuntimeError):
    """The requested path is outside what this tool may read."""


def _jail(root: Path, requested: str, allowed_suffixes: tuple[str, ...]) -> Path:
    """Resolve `requested` under `root`, or refuse."""
    if not requested or not requested.strip():
        raise PathRefused("a path is required")
    candidate = Path(requested.strip())
    if candidate.is_absolute():
        raise PathRefused(
            f"refusing absolute path {requested!r}; paths are relative to {root.name}/"
        )

    root_resolved = root.resolve()
    target = (root_resolved / candidate).resolve()

    # Layer 1 — containment, checked after resolution so `..` and symlinks
    # cannot walk out.
    if not target.is_relative_to(root_resolved):
        raise PathRefused(
            f"refusing {requested!r}: it resolves to {target}, outside {root_resolved}"
        )

    # Layer 3 — the unconditional denylist (checked before the extension
    # allowlist so the error names the real reason).
    name = target.name.lower()
    if name in FORBIDDEN_NAMES:
        raise PathRefused(f"refusing {requested!r}: {name!r} is never readable")
    if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        raise PathRefused(f"refusing {requested!r}: this file type is never readable")
    if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        raise PathRefused(f"refusing {requested!r}: this file type is never readable")

    # Layer 2 — the extension allowlist.
    if target.suffix.lower() not in allowed_suffixes:
        raise PathRefused(
            f"refusing {requested!r}: this tool serves only "
            f"{', '.join(allowed_suffixes)} files"
        )

    if not target.exists():
        raise PathRefused(f"{requested!r} does not exist under {root_resolved.name}/")
    if not target.is_file():
        raise PathRefused(f"{requested!r} is not a file")
    return target


def _read_capped(path: Path, trunc: Truncation) -> str:
    size = path.stat().st_size
    text = path.read_text(encoding="utf-8", errors="replace")
    if size > MAX_FILE_BYTES:
        text = text[:MAX_FILE_BYTES] + "\n…[file truncated]"
        trunc.note(f"file: truncated to {MAX_FILE_BYTES} bytes of {size}")
    return text


def read_runbook(docs_root: Path, path: str | None = None) -> dict[str, Any]:
    """Read a markdown document under `docs/`, or list what is available.

    Called with no path it returns the index, which is the call the model
    should make first — guessing a filename and getting a refusal teaches it
    nothing, while the index tells it exactly what exists.
    """
    trunc = Truncation()
    root = Path(docs_root)
    if not root.exists():
        raise PathRefused(f"docs root {root} does not exist")

    if not path:
        available = sorted(
            str(p.relative_to(root.resolve()))
            for p in root.resolve().rglob("*.md")
            if p.is_file()
        )
        prov = Provenance(
            source="repo", query=f"index of {root.name}/**/*.md", endpoint=str(root)
        )
        return {
            "index": True,
            "documents": available,
            "document_count": len(available),
            "provenance": prov.reference(),
            "citation_id": prov.citation_id(),
            "truncated": False,
        }

    target = _jail(root, path, (".md",))
    text = _read_capped(target, trunc)
    relative = target.relative_to(root.resolve())

    prov = Provenance(
        source="repo",
        query=f"{root.name}/{relative}",
        endpoint=str(target),
        extra={"lines": text.count("\n") + 1},
    )
    payload = {
        "index": False,
        "path": f"{root.name}/{relative}",
        "content": text,
        "provenance": prov.reference(),
        "citation_id": prov.citation_id(),
    }
    payload.update(trunc.as_payload())
    return enforce_result_bytes(payload, trunc)


def get_dashboard(dashboards_root: Path, name: str | None = None) -> dict[str, Any]:
    """Read a Grafana dashboard definition, or list the available ones.

    Returns a *panel summary* rather than the raw JSON by default. A dashboard
    file is mostly layout — gridPos, field overrides, colour thresholds — and
    the question a copilot is being asked is almost always "what does this
    system measure, and with which query", which is the panel titles and their
    targets. Handing over 60KB of layout to answer that would cost real money
    for no added signal.
    """
    trunc = Truncation()
    root = Path(dashboards_root)
    if not root.exists():
        raise PathRefused(f"dashboards root {root} does not exist")

    if not name:
        available = sorted(p.stem for p in root.resolve().glob("*.json") if p.is_file())
        prov = Provenance(
            source="repo", query=f"index of {root.name}/*.json", endpoint=str(root)
        )
        return {
            "index": True,
            "dashboards": available,
            "dashboard_count": len(available),
            "provenance": prov.reference(),
            "citation_id": prov.citation_id(),
            "truncated": False,
        }

    filename = name if name.endswith(".json") else f"{name}.json"
    target = _jail(root, filename, (".json",))
    raw = _read_capped(target, trunc)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PathRefused(f"{filename!r} is not valid JSON: {exc}") from None

    panels = []
    for panel in _iter_panels(parsed):
        panels.append(
            {
                "title": panel.get("title"),
                "type": panel.get("type"),
                "description": panel.get("description"),
                "queries": [
                    t.get("expr") or t.get("query")
                    for t in (panel.get("targets") or [])
                    if t.get("expr") or t.get("query")
                ],
            }
        )

    prov = Provenance(
        source="repo",
        query=f"{root.name}/{target.name}",
        endpoint=str(target),
        extra={"panel_count": len(panels)},
    )
    payload = {
        "index": False,
        "dashboard": target.stem,
        "title": parsed.get("title"),
        "description": parsed.get("description"),
        "tags": parsed.get("tags", []),
        "panel_count": len(panels),
        "panels": panels,
        "provenance": prov.reference(),
        "citation_id": prov.citation_id(),
    }
    payload.update(trunc.as_payload())
    return enforce_result_bytes(payload, trunc)


def _iter_panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten panels, including those nested inside collapsed rows."""
    found: list[dict[str, Any]] = []
    for panel in dashboard.get("panels") or []:
        if panel.get("type") == "row":
            found.extend(panel.get("panels") or [])
        else:
            found.append(panel)
    return found
