#!/usr/bin/env python3
"""Fail if the chart sets a DAY2_* env var no service actually reads.

A typo'd env name is the quietest config bug there is: pydantic-settings is
configured with `extra="ignore"`, so `DAY2_BATCH_SIZ` is not an error — the
worker starts happily on the default and the wrong batch size only shows up as
a throughput number nobody is watching. Nothing else in CI catches it: `helm
lint` and `helm template` both render it fine, and the unit tests never see the
chart.

So this compares the two halves directly. The chart's rendered `DAY2_*` env
names must each correspond to a field on that service's pydantic `Settings`.
The field list is read out of the source with `ast` rather than by importing
it, so this runs in the `helm` job with no dependencies installed and no
application import path to set up.

Usage:  python3 deploy/helm/scripts/check_env_contract.py [chart_dir]
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# (service, rendered template, config module holding its Settings)
SERVICES = (
    ("api", "templates/api.yaml", "app/api/api/config.py"),
    ("worker", "templates/worker.yaml", "app/worker/worker/config.py"),
)

# Env vars the chart sets that are deliberately not `Settings` fields.
KNOWN_NON_SETTINGS_ENV = {
    # Injected from the Postgres Secret and interpolated into DAY2_DATABASE_URL
    # by the chart itself (see `day2.databaseUrlTemplate`); no service reads it.
    "DAY2_POSTGRES_PASSWORD",
}

ENV_NAME = re.compile(r"^\s*-\s*name:\s*(DAY2_[A-Z0-9_]+)\s*$", re.MULTILINE)


def settings_fields(config_path: pathlib.Path) -> set[str]:
    """Field names on the `Settings` class, as DAY2_-prefixed env var names."""
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return {
                f"DAY2_{stmt.target.id.upper()}"
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise SystemExit(f"no `Settings` class found in {config_path}")


def rendered_env(chart_dir: pathlib.Path, template: str) -> set[str]:
    result = subprocess.run(
        [
            "helm", "template", "day2", str(chart_dir),
            "-f", str(chart_dir / "values-local.yaml"),
            "--show-only", template,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"helm template failed for {template}:\n{result.stderr}")
    return set(ENV_NAME.findall(result.stdout))


def main() -> int:
    chart_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "deploy/helm"
    failures: list[str] = []

    for service, template, config in SERVICES:
        declared = settings_fields(REPO_ROOT / config)
        used = rendered_env(chart_dir, template)
        unknown = sorted(used - declared - KNOWN_NON_SETTINGS_ENV)
        print(f"{service}: {len(used)} env vars in the chart, {len(declared)} settings fields")
        for name in unknown:
            near = sorted(declared, key=lambda f: -_overlap(f, name))[:1]
            hint = f" (closest field: {near[0]})" if near else ""
            failures.append(
                f"{service}: chart sets {name}, which {service} does not read{hint}"
            )

    if failures:
        print("\nChart/config contract violated:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nEither the env name is a typo, or the service needs a matching "
            "Settings field.",
            file=sys.stderr,
        )
        return 1

    print("\nChart/config env contract OK.")
    return 0


def _overlap(a: str, b: str) -> int:
    """Crude prefix-similarity, only used to suggest the likely intended name."""
    return sum(1 for x, y in zip(a, b, strict=False) if x == y)


if __name__ == "__main__":
    raise SystemExit(main())
