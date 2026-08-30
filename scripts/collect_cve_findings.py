#!/usr/bin/env python3
"""Flatten trivy's per-service SBOM scan reports into one list of findings.

Run by `.github/workflows/sbom-rescan.yml` between the scanner and the agent.
It exists so the agent receives one small, stable, service-annotated document
instead of three large trivy reports whose shape it would have to learn.

Three things happen here and nothing else:

* **Flatten.** Every `Results[].Vulnerabilities[]` entry across every report
  becomes one record, tagged with the service whose SBOM it came from.
* **Merge by CVE.** The same CVE in the same package usually appears in more
  than one image — `api` and `worker` share a base. One record per
  (CVE, package, installed version), listing every service affected, is both
  the truthful shape and the one that makes "one PR per CVE" natural.
* **Report what is there, and only that.** Fields trivy did not supply are
  `null`, never guessed. `layer` is present when the scanned SBOM carried it —
  trivy's own CycloneDX does, which is one more reason the rescan reads that
  copy rather than the syft SPDX beside it — and null when it did not. Null
  means "not recorded", never "no layer". Mapping a package to the *Dockerfile
  stage* that installs it stays the agent's job, done against the Dockerfile,
  where the answer is knowable rather than inferred.

No severity filtering happens here: trivy was already invoked with the exact
filter `ci.yml` gates on, and re-filtering in a second place is how the two
drift apart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _finding_key(vuln: dict[str, Any]) -> tuple[str, str, str]:
    return (
        vuln.get("VulnerabilityID", ""),
        vuln.get("PkgName", ""),
        vuln.get("InstalledVersion", ""),
    )


def collect(report_dir: Path) -> dict[str, Any]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    for report_path in sorted(report_dir.glob("*.json")):
        service = report_path.stem
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for result in report.get("Results") or []:
            for vuln in result.get("Vulnerabilities") or []:
                key = _finding_key(vuln)
                if not key[0]:
                    continue
                record = merged.get(key)
                if record is None:
                    record = {
                        "cve": key[0],
                        "package": key[1],
                        "installed": key[2],
                        "fixed": vuln.get("FixedVersion") or None,
                        "severity": vuln.get("Severity") or None,
                        "title": vuln.get("Title") or None,
                        "url": vuln.get("PrimaryURL") or None,
                        "purl": (vuln.get("PkgIdentifier") or {}).get("PURL"),
                        # Present when the SBOM carried it. Null means "not
                        # recorded", never "no layer".
                        "layer": (vuln.get("Layer") or {}).get("Digest") or None,
                        "pkg_class": result.get("Class") or None,
                        "pkg_type": result.get("Type") or None,
                        "service": [],
                    }
                    merged[key] = record
                if service not in record["service"]:
                    record["service"].append(service)

    findings = sorted(
        merged.values(),
        # CRITICAL first, then by CVE id, so the order is deterministic and the
        # most urgent finding is the one a reader sees first.
        key=lambda f: (f["severity"] != "CRITICAL", f["cve"]),
    )
    for finding in findings:
        finding["service"].sort()
    return {"findings": findings}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: collect_cve_findings.py <trivy-report-dir>", file=sys.stderr)
        return 2
    json.dump(collect(Path(argv[0])), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
