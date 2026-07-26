"""Cost sentinel — read-only inventory of anything in the account that costs money.

Enforces CLAUDE.md rule 1 (~$15/mo ceiling, no NAT/EKS/managed DBs) and rule 6
(ephemeral cloud: destroy leaves zero orphans) by looking at what actually
exists, not at what Terraform believes exists.

Deliberately resource-based rather than spend-based. Cost Explorer bills per
request and its data lags roughly a day — by the time spend shows up, a node
left running has already been up overnight. `Describe*`/`List*` calls are free,
immediate, and answer the question that actually matters here: *is anything
running right now that shouldn't be?* Every call this makes is a describe or a
list; the sentinel reports and never mutates (rule 3 — it proposes, Pascal acts).

Prices are read live from DescribeSpotPriceHistory. Nothing is hardcoded and
nothing is estimated from memory (rule 5) — where a real price is unavailable,
the finding says so instead of guessing.

Driven through the AWS CLI rather than boto3 so it needs no dependency install:
the CLI is preinstalled on GitHub runners and on the operator's machine, which
means this script can be run by hand against a live account before it is ever
trusted in CI. Set AWS_CLI to point at a non-default binary.

Exit codes: 0 clean, 1 findings, 2 the sentinel itself failed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

# A run older than this is assumed forgotten rather than in use.
DEFAULT_MAX_RUNTIME_HOURS = 8.0

SEVERITY_ORDER = {"violation": 0, "waste": 1, "runtime": 2, "unchecked": 3}

DENIAL_MARKERS = (
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "not authorized to perform",
)


@dataclass
class Finding:
    severity: str  # violation | waste | runtime | unchecked
    resource: str
    detail: str


@dataclass
class Report:
    region: str
    findings: list[Finding] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    hourly_usd: float = 0.0
    priced: bool = True
    live_instances: int = 0

    def add(self, severity: str, resource: str, detail: str) -> None:
        self.findings.append(Finding(severity, resource, detail))


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_launch_time(value: str) -> dt.datetime | None:
    """CLI emits ISO-8601; tolerate the trailing Z that fromisoformat rejects."""
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def resolve_cli() -> str:
    """Resolve the AWS CLI binary, allowing an override only to another AWS CLI.

    AWS_CLI exists so the script can be run by hand against a live account (under
    WSL that means pointing at `aws.exe`). Constraining the basename keeps it a
    convenience rather than an arbitrary-binary execution primitive: the override
    can select *which* aws to run, never *what* to run. Every other element of
    every command below is a literal in this file, and nothing is passed through
    a shell.
    """
    raw = os.environ.get("AWS_CLI", "aws")
    if os.path.basename(raw).lower() not in {"aws", "aws.exe"}:
        raise ValueError(
            f"AWS_CLI must name an aws binary (aws or aws.exe), got {raw!r}"
        )
    resolved = shutil.which(raw)
    if resolved is None:
        raise FileNotFoundError(f"AWS CLI not found: {raw!r}")
    return resolved


def aws(report: Report, label: str, *args: str, region: str | None = None) -> Any | None:
    """Run one read-only AWS CLI call, returning parsed JSON.

    A sentinel that dies on one missing grant silently stops checking everything
    after it, so a denial is recorded as a visible gap and the run continues.
    """
    try:
        cli = resolve_cli()
    except (ValueError, FileNotFoundError) as exc:
        report.add("unchecked", label, str(exc))
        return None

    cmd = [cli, *args, "--output", "json"]
    if region:
        cmd += ["--region", region]
    try:
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        # cmd[0] is validated by resolve_cli() above (basename allowlisted, then
        # resolved through PATH); every other element is a literal constant in
        # this file. No shell, list form — there is no injection surface.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except OSError as exc:
        report.add("unchecked", label, f"could not execute the AWS CLI: {exc}")
        return None
    except subprocess.TimeoutExpired:
        report.add("unchecked", label, "call timed out after 90s")
        return None

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if any(marker in err for marker in DENIAL_MARKERS):
            report.add(
                "unchecked",
                label,
                "not permitted — this grant is missing from the sentinel policy",
            )
        else:
            reason = err.splitlines()[-1] if err else "unknown error"
            report.add("unchecked", label, f"call failed: {reason}")
        return None

    # Some CLI calls legitimately print nothing on an empty result set.
    if not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        report.add("unchecked", label, "response was not valid JSON")
        return None


def spot_price(report: Report, region: str, instance_type: str, az: str) -> float | None:
    """Current spot price for this exact type+AZ, straight from the API."""
    resp = aws(
        report,
        "ec2:DescribeSpotPriceHistory",
        "ec2", "describe-spot-price-history",
        "--instance-types", instance_type,
        "--availability-zone", az,
        "--product-descriptions", "Linux/UNIX",
        "--max-items", "1",
        region=region,
    )
    if not resp or not resp.get("SpotPriceHistory"):
        return None
    try:
        return float(resp["SpotPriceHistory"][0]["SpotPrice"])
    except (KeyError, IndexError, ValueError):
        return None


def check_instances(report: Report, region: str, max_hours: float) -> None:
    resp = aws(
        report,
        "ec2:DescribeInstances",
        "ec2", "describe-instances",
        "--filters", "Name=instance-state-name,Values=pending,running",
        region=region,
    )
    if resp is None:
        return

    instances = [i for r in resp.get("Reservations", []) for i in r.get("Instances", [])]
    report.live_instances = len(instances)
    if not instances:
        report.facts.append("No running or pending EC2 instances.")
        return

    for inst in instances:
        iid = inst["InstanceId"]
        itype = inst.get("InstanceType", "?")
        az = inst.get("Placement", {}).get("AvailabilityZone", "")
        launched = _parse_launch_time(inst.get("LaunchTime", ""))
        age_h = (_utcnow() - launched).total_seconds() / 3600 if launched else 0.0
        lifecycle = inst.get("InstanceLifecycle", "on-demand")

        price = spot_price(report, region, itype, az) if lifecycle == "spot" else None
        if price is not None:
            report.hourly_usd += price
            price_txt = f"${price:.4f}/hr (live spot quote)"
        else:
            report.priced = False
            price_txt = "price not retrieved — burn total below is incomplete"

        line = f"`{iid}` {itype} ({lifecycle}) in {az}, up {age_h:.1f}h, {price_txt}"
        if age_h > max_hours:
            report.add(
                "runtime",
                iid,
                f"{line} — over the {max_hours:g}h threshold. This stack is meant to be "
                f"ephemeral; if the demo is finished, `terraform destroy` it.",
            )
        else:
            report.facts.append(line)


def check_orphans(report: Report, region: str) -> None:
    """Things that survive a botched destroy and quietly bill forever."""
    vols = aws(
        report,
        "ec2:DescribeVolumes",
        "ec2", "describe-volumes",
        "--filters", "Name=status,Values=available",
        region=region,
    )
    if vols is not None:
        for v in vols.get("Volumes", []):
            report.add(
                "waste",
                v["VolumeId"],
                f"unattached {v.get('Size')} GiB {v.get('VolumeType')} volume — "
                f"billed while attached to nothing. Orphan from a destroy.",
            )
        if not vols.get("Volumes"):
            report.facts.append("No unattached EBS volumes.")

    addrs = aws(
        report, "ec2:DescribeAddresses", "ec2", "describe-addresses", region=region
    )
    if addrs is not None:
        idle = [a for a in addrs.get("Addresses", []) if not a.get("AssociationId")]
        for a in idle:
            report.add(
                "waste",
                a.get("AllocationId", a.get("PublicIp", "?")),
                f"Elastic IP {a.get('PublicIp')} allocated but associated with nothing — "
                f"AWS bills idle EIPs by the hour.",
            )
        if not idle:
            report.facts.append("No idle Elastic IPs.")

    snaps = aws(
        report,
        "ec2:DescribeSnapshots",
        "ec2", "describe-snapshots", "--owner-ids", "self",
        region=region,
    )
    if snaps is not None:
        for s in snaps.get("Snapshots", []):
            report.add(
                "waste",
                s["SnapshotId"],
                f"self-owned snapshot ({s.get('VolumeSize')} GiB) — nothing in this "
                f"project creates snapshots, so this is a leftover.",
            )
        if not snaps.get("Snapshots"):
            report.facts.append("No self-owned snapshots.")


def check_rule_one(report: Report, region: str) -> None:
    """Tripwires for the architecture constraints that must never be crossed."""
    nats = aws(
        report, "ec2:DescribeNatGateways", "ec2", "describe-nat-gateways", region=region
    )
    if nats is not None:
        live = [
            n for n in nats.get("NatGateways", [])
            if n.get("State") in {"pending", "available"}
        ]
        for n in live:
            report.add(
                "violation",
                n["NatGatewayId"],
                "NAT gateway exists — CLAUDE.md rule 1 forbids it (~$32/mo alone, "
                "more than the entire ceiling).",
            )
        if not live:
            report.facts.append("No NAT gateways.")

    clusters = aws(report, "eks:ListClusters", "eks", "list-clusters", region=region)
    if clusters is not None:
        for c in clusters.get("clusters", []):
            report.add(
                "violation",
                c,
                "EKS cluster exists — CLAUDE.md rule 1 forbids it (~$73/mo control "
                "plane; see ADR 0001).",
            )
        if not clusters.get("clusters"):
            report.facts.append("No EKS clusters.")

    dbs = aws(
        report, "rds:DescribeDBInstances", "rds", "describe-db-instances", region=region
    )
    if dbs is not None:
        for d in dbs.get("DBInstances", []):
            report.add(
                "violation",
                d["DBInstanceIdentifier"],
                f"managed database ({d.get('Engine')}) exists — CLAUDE.md rule 1 "
                f"forbids it; Postgres runs in-cluster.",
            )
        if not dbs.get("DBInstances"):
            report.facts.append("No managed databases.")


def check_leftover_network(report: Report, region: str) -> None:
    """A non-default VPC outliving its instances means a destroy did not finish.

    While the stack is deliberately up, its VPC is expected and not a finding —
    flagging it then would cry wolf on every run of a healthy demo. It only
    becomes an orphan once nothing is running inside it.
    """
    vpcs = aws(report, "ec2:DescribeVpcs", "ec2", "describe-vpcs", region=region)
    if vpcs is None:
        return
    custom = [v for v in vpcs.get("Vpcs", []) if not v.get("IsDefault")]

    if not custom:
        report.facts.append("No leftover non-default VPCs.")
        return

    if report.live_instances:
        for v in custom:
            report.facts.append(
                f"Non-default VPC {v['VpcId']} ({v.get('CidrBlock')}) — in use by "
                f"{report.live_instances} running instance(s), so not an orphan."
            )
        return

    for v in custom:
        report.add(
            "waste",
            v["VpcId"],
            f"non-default VPC {v.get('CidrBlock')} still present with no running "
            f"instances — free in itself, but it means a destroy did not complete "
            f"(rule 6).",
        )


def render(report: Report, max_hours: float) -> str:
    out: list[str] = []
    ranked = sorted(report.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    real = [f for f in ranked if f.severity != "unchecked"]

    if real:
        out.append(f"## Cost sentinel: {len(real)} finding(s) in `{report.region}`\n")
    else:
        out.append(f"## Cost sentinel: clean in `{report.region}`\n")

    if report.hourly_usd > 0:
        daily = report.hourly_usd * 24
        monthly = report.hourly_usd * 730
        qualifier = "" if report.priced else " (incomplete — some prices unavailable)"
        out.append(
            f"**Compute burn{qualifier}: ${report.hourly_usd:.4f}/hr "
            f"≈ ${daily:.2f}/day ≈ ${monthly:.2f}/mo if left running.** "
            f"Live spot quotes; EBS and data transfer are not included.\n"
        )

    labels = {
        "violation": "🚨 Hard-constraint violation",
        "waste": "💸 Orphaned / idle resource",
        "runtime": "⏳ Running longer than expected",
        "unchecked": "❓ Could not check",
    }
    for sev in ("violation", "waste", "runtime", "unchecked"):
        group = [f for f in ranked if f.severity == sev]
        if not group:
            continue
        out.append(f"### {labels[sev]}\n")
        for f in group:
            out.append(f"- **{f.resource}** — {f.detail}")
        out.append("")

    if report.facts:
        out.append("<details><summary>Checks that passed</summary>\n")
        for fact in report.facts:
            out.append(f"- {fact}")
        out.append("\n</details>\n")

    out.append(
        f"---\nThreshold: {max_hours:g}h. The sentinel is read-only — it reports, it "
        f"does not stop or delete anything (CLAUDE.md rule 3)."
    )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only AWS cost/orphan sentinel.")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-southeast-2"))
    ap.add_argument(
        "--max-runtime-hours",
        type=float,
        default=float(os.environ.get("MAX_RUNTIME_HOURS", DEFAULT_MAX_RUNTIME_HOURS)),
    )
    ap.add_argument("--json-out", help="also write the findings as JSON here")
    args = ap.parse_args()

    report = Report(region=args.region)
    check_instances(report, args.region, args.max_runtime_hours)
    check_orphans(report, args.region)
    check_rule_one(report, args.region)
    check_leftover_network(report, args.region)

    # Every single check failing means the sentinel is broken, not that the
    # account is clean — that must not be reported as a pass.
    blind = report.findings and all(f.severity == "unchecked" for f in report.findings)
    if blind and not report.facts:
        print(render(report, args.max_runtime_hours))
        print("\nsentinel could not complete a single check", file=sys.stderr)
        return 2

    body = render(report, args.max_runtime_hours)
    print(body)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "region": report.region,
                    "hourly_usd": round(report.hourly_usd, 6),
                    "fully_priced": report.priced,
                    "findings": [vars(f) for f in report.findings],
                    "passed": report.facts,
                },
                fh,
                indent=2,
            )

    actionable = [f for f in report.findings if f.severity != "unchecked"]
    return 1 if actionable else 0


if __name__ == "__main__":
    sys.exit(main())
