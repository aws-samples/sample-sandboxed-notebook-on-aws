#!/usr/bin/env python3
# test-kind: gated
"""
NetworkEgressTest.py — provision AWS Network Firewall egress filtering (Option A
from NETWORK_EGRESS.md) end-to-end, launch a real MicroVM wired to it, and prove
the domain allow/deny filtering actually works.

WHAT THIS DOES
--------------
Provisions, in dependency order: VPC -> 3 subnets (private/firewall/public) -> IGW
-> NAT Gateway -> Network Firewall domain-allowlist rule group -> firewall policy
-> Network Firewall -> route tables/routes -> operator IAM role -> security group
-> VPC egress network connector (lambda-core create-network-connector) -> a MicroVM
launched with ONLY the VPC egress connector attached (no INTERNET_EGRESS). It then
runs a connectivity smoke test (the gate — if the egress path isn't forwarding the
whole run is INCONCLUSIVE) followed by a declarative 10-case allow/deny + edge-case
MATRIX from inside the MicroVM, and reports PASS/FAIL/INCONCLUSIVE/KNOWN_LIMITATION
per case with evidence, plus an aggregate run verdict and exit code.

The matrix covers: apex-allow, reach-an-allowed-host, multiple deny domains,
wildcard-suffix subdomain allow (.amazonaws.com), look-alike suffix-confusion deny
(a resolvable host containing but not under the allowed suffix),
an allow-then-redirect-to-denied trap, plain-HTTP (port 80) HTTP_HOST filtering, a
documented forged-SNI known-limitation boundary, and an NXDOMAIN case that proves a
non-firewall DNS failure is NOT counted as a block.

DENY verdicts are ALERT-LOG-FIRST: each deny-style case reads the Network Firewall
ALERT log for a matching drop with a SHORT bounded poll FIRST, and only falls back
to the longer CloudWatch metric poll if the log does not confirm within that short
window. classify_deny()'s verdict rules (log evidence primary, metrics fallback,
INCONCLUSIVE semantics) are unchanged — only WHEN each query runs changed.

This is a free-standing, one-off operational script — it is NOT wired into the proxy's permanent
code path and has no dependency on the proxy running. It launches the MicroVM
directly via boto3 lambda-microvms.run_microvm.

PREREQUISITES
--------------
Environment variables (same convention as
tests/eternal/test_reactive_rotation.py — source scripts/config.sh first, or set
these by hand):
  AWS_REGION              AWS region (default "us-west-2")
  AWS_CLI_PROFILE         Named AWS CLI profile (default "default" -> boto3 default
                          credential chain, no explicit profile)
  IMAGE_ARN / MICROVM_IMAGE_ARN
                          Arch-agnostic base image ARN. This script appends
                          "-{arch}-{mem}" itself (same convention as config.sh's
                          image_arn_for()). REQUIRED — the script refuses to launch
                          without it.
  EXEC_ROLE_ARN / MICROVM_EXEC_ROLE_ARN
                          Execution role ARN for the MicroVM. REQUIRED — a VM with
                          no execution role has no AWS credentials; this script
                          refuses to launch a broken VM.
  MICROVM_INGRESS_CONNECTOR
                          Optional override for the standard HTTP_INGRESS connector
                          ARN (defaults to the AWS-managed one, same construction as
                          proxy/platform/microvm_manager.py).
  TEST_ARCH / DEFAULT_IMAGE_ARCH
                          Default for --arch (TEST_ARCH wins; else arm64). Set
                          TEST_ARCH=x86_64 to run against notebook-microvm-x86_64-<mem>.

COST WARNING
-------------
This test provisions BILLABLE infrastructure: an AWS Network Firewall endpoint
(~$0.395/hr), a NAT Gateway (typically $0/hr in this topology — see
NETWORK_EGRESS.md's "Cost Considerations" section, waived when co-located with the
firewall) plus data processing, and MicroVM compute while it runs. The script
prints a cost estimate and requires --yes/-y or an interactive confirmation before
creating anything. Remember to run NetworkEgressTeardown.py when you are done —
leaving the firewall/NAT GW running accrues cost indefinitely.

INVOCATION ORDER
-----------------
    source scripts/config.sh      # or export the env vars above by hand
    python3 tests/NetworkEgress/NetworkEgressTest.py --yes
    # ... note the printed run id ...
    python3 tests/NetworkEgress/NetworkEgressTeardown.py --run-id <id> --yes

Re-running NetworkEgressTest.py (same or omitted --run-id) resumes/rejects per the
exact 6-case rule implemented in this script (see the --run-id handling).

EXECUTION CHANNEL (how the allow/deny HTTPS requests are actually run)
------------------------------------------------------------------------
The allow/deny assertions run as a short Python snippet sent to the repo's own
in-VM `/execute` endpoint (app/notebook/code_engine.py), called DIRECTLY against
the MicroVM's own HTTPS endpoint — NOT through the proxy's `/proxy/execute`
forwarding route. This mirrors exactly what
proxy/platform/microvm_manager.py's `get_auth_token()` (line ~827) and its
various `/execute`-style callers (e.g. proxy/platform/routes/sessions.py:263) do:

  1. Auth: a self-minted token from
     boto3.client("lambda-microvms").create_microvm_auth_token(
         microvmIdentifier=<id>, expirationInMinutes=30,
         allowedPorts=[{"allPorts": {}}])["authToken"]["X-aws-proxy-auth"]
  2. Request: POST https://{endpoint}/execute with header
     "X-aws-proxy-auth: <token>" and JSON body {"code": "<snippet>"}.
  3. Response: a JSON object with (at least) "success" (bool), "output" (str,
     stdout captured during execution), and "error" (str|None) — the exact shape
     app/notebook/code_engine.py's `_code_result_dict()` returns.

CORRECTED PRECEDENT NOTE (round-4 review finding): earlier design language claimed
every test under tests/common/, tests/checkpoint/, tests/eternal/ already proves
this "call /execute directly against the VM" pattern end-to-end. That claim is
FALSE — those tests (e.g. tests/eternal/test_reactive_rotation.py's execute_code())
call `/proxy/execute` on the PROXY, which forwards to the VM's own `/execute` using
this same auth-token mechanism internally (see proxy/platform/routes/sessions.py:
263-267). They do not call the VM's endpoint directly. The actual (and only)
in-repo precedent for calling a VM's own endpoint directly, bypassing the proxy, is
proxy/platform/microvm_manager.py itself (e.g. its `/health`-polling and
`/restore-state` calls at lines ~671-690) — internal platform code, not a test.
The underlying DESIGN DECISION to call /execute directly is still correct (this
script has no dependency on a running proxy); only the "proven by existing tests"
justification was wrong, and is corrected here.
"""

import os
import sys
import json
import time
import uuid
import argparse
import tempfile

import boto3
import requests
from botocore.exceptions import ClientError

# ── Config (from environment / scripts/config.sh) ──────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
AWS_PROFILE = os.environ.get("AWS_CLI_PROFILE", "default")
IMAGE_ARN_BASE = os.environ.get("IMAGE_ARN", os.environ.get("MICROVM_IMAGE_ARN", ""))
EXEC_ROLE_ARN = os.environ.get("EXEC_ROLE_ARN", os.environ.get("MICROVM_EXEC_ROLE_ARN", ""))
INGRESS_CONNECTOR = os.environ.get(
    "MICROVM_INGRESS_CONNECTOR",
    f"arn:aws:lambda:{AWS_REGION}:aws:network-connector:aws-network-connector:HTTP_INGRESS",
)

PROJECT_TAG = "microvm-network-egress-test"
STATE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(STATE_DIR, ".network_egress_test_state.json")
LOCK_FILE = STATE_FILE + ".lock"

VPC_CIDR = "10.0.0.0/16"
PRIVATE_SUBNET_CIDR = "10.0.1.0/24"
FIREWALL_SUBNET_CIDR = "10.0.2.0/24"
PUBLIC_SUBNET_CIDR = "10.0.3.0/24"

DEFAULT_ALLOWLIST = [
    ".amazonaws.com", "pypi.org", "files.pythonhosted.org",
    "github.com", "api.github.com", "raw.githubusercontent.com",
]
DEFAULT_DENY_DOMAINS = ["example.com", "example.org", "example.net"]
DEFAULT_DENY_DOMAIN = DEFAULT_DENY_DOMAINS[0]  # alias kept for any single-domain reference

# Domain-list rules consume rule-group capacity per Target; the Targets list is
# now 6 entries plus any --allowlist overrides, so size generously (AWS's own
# guidance: capacity cannot be increased later without recreating the rule
# group, so it is worth over-provisioning up front).
RULE_GROUP_CAPACITY = 200

# VM lifecycle states (reused verbatim from proxy/platform/microvm_manager.py:62).
VM_DEAD_STATES = {"TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"}

# Deny-case: a connection-level failure is a PASS *candidate* (the firewall may
# have blocked it before the HTTP layer completed) — but per investigate-l7-egress-
# block/report.md, a client-side timeout is also exactly what a non-delivery
# failure (traffic never reaching the firewall/connector data plane) looks like.
# A DNS-resolution failure is explicitly NOT a pass — it would mean we tested
# something other than the firewall's SNI/Host matching.
CONNECTION_LEVEL_ERRORS = ("ConnectionError", "ConnectTimeout", "SSLError", "ReadTimeout")
DNS_ERROR_MARKERS = ("NameResolutionError", "gaierror", "nodename nor servname")

# For GeneratedRulesType: ALLOWLIST, the implicit "deny everything not listed"
# rule is a silent DROP (not an active REJECT/TCP-reset) — see
# investigate-l7-egress-block/report.md section 4. A ConnectTimeout/ReadTimeout
# is therefore the CORRECT expected client-side symptom for a real block under
# this rule type. A ConnectionError/SSLError arising from an actual TCP RST
# would instead indicate a REJECT-style rule fired. These are recorded as
# distinct "deny_mechanism" values (not folded into one PASS) so a human
# reviewing results later doesn't have to re-derive it from raw exception text.
DROP_TIMEOUT_ERRORS = ("ConnectTimeout", "ReadTimeout")
REJECT_RESET_ERRORS = ("ConnectionError", "SSLError")

# CloudWatch metric window padding either side of the deny-case request, to
# absorb clock skew between the test host and AWS, plus the few-minute lag
# AWS's own metric reference documents for these TLS metrics (they only
# publish nonzero datapoints, and may lag the actual traffic by a few minutes).
METRIC_WINDOW_PADDING_SECONDS = 120
METRIC_POLL_TIMEOUT_SECONDS = 300
METRIC_POLL_INTERVAL_SECONDS = 20


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _session():
    if AWS_PROFILE and AWS_PROFILE != "default":
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


# ── Lock file (single-host guard; see design.md "State hand-off file") ─────

def acquire_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
        except Exception:
            pid = None
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            log(f"❌ another instance (PID {pid}) is already operating on the state "
                f"file — wait for it to finish or confirm it's dead and remove "
                f"{LOCK_FILE} manually.")
            return False
        log(f"⚠ stale lock file found (PID {pid} not running) — removing and proceeding.")
        try:
            os.remove(LOCK_FILE)
        except FileNotFoundError:
            pass
        return acquire_lock()


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


# ── State file (atomic read/write) ──────────────────────────────────────────

def _read_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state):
    """Atomic write: tempfile in the same dir + os.replace."""
    fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, prefix=".network_egress_test_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _append_resource(run_id, record):
    """Single write path for recording a created resource. Called immediately
    after every successful AWS create call, by both the happy path and every
    exception handler, so a resource is recorded the moment it exists."""
    record = dict(record)
    record.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    state = _read_state()
    entry = state.setdefault(run_id, {})
    entry.setdefault("resources", []).append(record)
    _write_state(state)


def _set_entry_fields(run_id, **fields):
    state = _read_state()
    entry = state.setdefault(run_id, {})
    entry.update(fields)
    _write_state(state)


# ── Idempotency: resolve the run id per the 6-case rule (design.md) ────────

def resolve_run_id(explicit_run_id):
    state = _read_state()
    incomplete = [rid for rid, e in state.items() if e.get("status") in ("in_progress", "failed")]
    completed = [rid for rid, e in state.items() if e.get("status") == "completed"]

    if explicit_run_id:
        if explicit_run_id in state:
            if state[explicit_run_id].get("status") == "completed":
                log(f"❌ run '{explicit_run_id}' already completed; use "
                    f"NetworkEgressTeardown.py --run-id {explicit_run_id} first, or "
                    f"pass a new --run-id to start a fresh run.")
                sys.exit(1)
            log(f"Resuming existing run '{explicit_run_id}' (status="
                f"{state[explicit_run_id].get('status')}).")
            return explicit_run_id, True
        log(f"Starting a fresh run under explicit id '{explicit_run_id}'.")
        return explicit_run_id, False

    if not incomplete:
        # mint a new run id (fresh stack) — a no-flag invocation never implicitly
        # resumes a completed run.
        if completed:
            log("No --run-id given and only completed runs exist — starting a NEW run "
                "(a no-flag invocation never resumes a completed run).")
        new_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        return new_id, False

    if len(incomplete) == 1:
        log(f"Resuming the single incomplete run '{incomplete[0]}'.")
        return incomplete[0], True

    log("❌ multiple incomplete runs exist — pass --run-id explicitly to disambiguate:")
    for rid in incomplete:
        log(f"    {rid} (status={state[rid].get('status')})")
    sys.exit(1)


# ── AWS safety / identity ───────────────────────────────────────────────────

def validate_run_id(run_id):
    import re
    if not re.match(r"^[a-zA-Z0-9-]{1,64}$", run_id):
        log(f"❌ --run-id '{run_id}' is invalid — must match ^[a-zA-Z0-9-]{{1,64}}$")
        sys.exit(1)


def tags_for(run_id, extra_name=None):
    tags = [{"Key": "Project", "Value": PROJECT_TAG}, {"Key": "RunId", "Value": run_id}]
    if extra_name:
        tags.append({"Key": "Name", "Value": extra_name})
    return tags


# ── Readiness polling helpers ───────────────────────────────────────────────

def poll_until(fn, is_ready, timeout, interval, label):
    """Call fn() repeatedly until is_ready(result) or timeout. Returns the last
    result (or raises TimeoutError)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if is_ready(last):
            return last
        time.sleep(interval)
    raise TimeoutError(f"{label} did not become ready within {timeout}s")


# ── Provisioning steps ──────────────────────────────────────────────────────

def already_created(state, run_id, rtype, **match):
    """Within-run idempotency: has this resource already been recorded for this
    run (so we can resume rather than re-create it)?"""
    entry = state.get(run_id, {})
    for r in entry.get("resources", []):
        if r.get("type") != rtype:
            continue
        if all(r.get(k) == v for k, v in match.items()):
            return r
    return None


# The MicroVM itself runs in AWS-managed infrastructure, NOT in our VPC — we only
# attach an egress network connector to OUR VPC. But the connector anchors its
# (Hyperplane-managed) ENIs in the subnet WE give it, and AWS requires that
# subnet's AZ to be one where the MicroVm compute type is serviceable (so the
# managed MicroVM can establish its egress path to that ENI). Not every AZ in a
# region qualifies: CreateNetworkConnector fails with "Availability zone <az-id>
# is not available for compute type MicroVm" if the connector subnet lands in an
# unsupported AZ. We saw usw2-az4 rejected in us-west-2. AWS exposes no "list AZs
# that support MicroVm" API, so we resolve an AZ by EXCLUDING the known-bad AZ IDs
# and pinning all three subnets to one qualifying zone (they share an AZ anyway so
# the firewall endpoint, NAT, and connector ENIs line up). Override with --az if
# AWS's offering differs.
MICROVM_UNSUPPORTED_AZ_IDS = {"usw2-az4"}


def resolve_connector_az(ec2, args):
    """Pick a single availability zone (zone NAME, e.g. us-west-2a) for the
    connector's VPC subnets — one where the MicroVm compute type is serviceable,
    so CreateNetworkConnector will accept it. Honors an explicit --az override;
    otherwise returns the first available zone whose AZ ID is not in
    MICROVM_UNSUPPORTED_AZ_IDS."""
    if getattr(args, "az", None):
        return args.az
    zones = ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]},
                 {"Name": "region-name", "Values": [AWS_REGION]}]
    )["AvailabilityZones"]
    for z in zones:
        if z.get("ZoneId") not in MICROVM_UNSUPPORTED_AZ_IDS:
            return z["ZoneName"]
    raise RuntimeError(
        f"no MicroVm-supported availability zone found in {AWS_REGION} "
        f"(all candidates were in the unsupported set {MICROVM_UNSUPPORTED_AZ_IDS}); "
        f"pass --az explicitly")


def provision(ec2, nf, lam_core, logs_client, run_id, args):
    state = _read_state()

    def have(rtype, **match):
        return already_created(state, run_id, rtype, **match)

    # 1. VPC
    rec = have("vpc")
    if rec:
        vpc_id = rec["id"]
        log(f"[resume] VPC {vpc_id} already created.")
    else:
        resp = ec2.create_vpc(
            CidrBlock=VPC_CIDR,
            TagSpecifications=[{"ResourceType": "vpc", "Tags": tags_for(run_id, f"microvm-egress-test-vpc-{run_id}")}],
        )
        vpc_id = resp["Vpc"]["VpcId"]
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
        _append_resource(run_id, {"type": "vpc", "id": vpc_id})
        log(f"Created VPC {vpc_id}")

    # 2. Subnets
    # Pin all three subnets to ONE AZ where the egress connector can anchor its
    # ENIs. If we let AWS auto-pick, the subnets can land in an AZ (e.g. usw2-az4)
    # where the MicroVm compute type isn't serviceable, and CreateNetworkConnector
    # later fails with "Availability zone ... is not available for compute type
    # MicroVm". Resolving the AZ up front avoids that. (The MicroVM runs in AWS's
    # own infra, not here — this AZ only governs where the connector's VPC ENIs go.)
    az = resolve_connector_az(ec2, args)
    log(f"Using availability zone {az} for connector subnets (MicroVm-serviceable)")
    subnet_ids = {}
    for role, cidr in (("private", PRIVATE_SUBNET_CIDR), ("firewall", FIREWALL_SUBNET_CIDR), ("public", PUBLIC_SUBNET_CIDR)):
        rec = have("subnet", role=role)
        if rec:
            subnet_ids[role] = rec["id"]
            log(f"[resume] {role} subnet {rec['id']} already created.")
            continue
        resp = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az,
            TagSpecifications=[{"ResourceType": "subnet", "Tags": tags_for(run_id, f"microvm-egress-test-{role}-{run_id}")}],
        )
        sid = resp["Subnet"]["SubnetId"]
        subnet_ids[role] = sid
        _append_resource(run_id, {"type": "subnet", "role": role, "id": sid})
        log(f"Created {role} subnet {sid} ({cidr}) in {az}")

    # 3. Internet Gateway
    rec = have("internet_gateway")
    if rec:
        igw_id = rec["id"]
        log(f"[resume] IGW {igw_id} already created.")
    else:
        resp = ec2.create_internet_gateway(
            TagSpecifications=[{"ResourceType": "internet-gateway", "Tags": tags_for(run_id, f"microvm-egress-test-igw-{run_id}")}],
        )
        igw_id = resp["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        _append_resource(run_id, {"type": "internet_gateway", "id": igw_id})
        log(f"Created and attached IGW {igw_id}")

    # 4. Elastic IP
    rec = have("eip")
    if rec:
        eip_alloc_id = rec["allocation_id"]
        log(f"[resume] EIP {eip_alloc_id} already allocated.")
    else:
        resp = ec2.allocate_address(
            Domain="vpc",
            TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": tags_for(run_id, f"microvm-egress-test-eip-{run_id}")}],
        )
        eip_alloc_id = resp["AllocationId"]
        _append_resource(run_id, {"type": "eip", "allocation_id": eip_alloc_id})
        log(f"Allocated EIP {eip_alloc_id}")

    # 5. NAT Gateway (async — poll to "available")
    rec = have("nat_gateway")
    if rec:
        nat_gw_id = rec["id"]
        log(f"[resume] NAT GW {nat_gw_id} already created.")
    else:
        resp = ec2.create_nat_gateway(
            SubnetId=subnet_ids["public"], AllocationId=eip_alloc_id,
            TagSpecifications=[{"ResourceType": "natgateway", "Tags": tags_for(run_id, f"microvm-egress-test-nat-{run_id}")}],
        )
        nat_gw_id = resp["NatGateway"]["NatGatewayId"]
        _append_resource(run_id, {"type": "nat_gateway", "id": nat_gw_id})
        log(f"Created NAT GW {nat_gw_id} — polling for 'available' (timeout 5 min)...")
        poll_until(
            lambda: ec2.describe_nat_gateways(NatGatewayIds=[nat_gw_id])["NatGateways"][0],
            lambda ngw: ngw["State"] == "available",
            timeout=300, interval=10, label="NAT Gateway",
        )
        log(f"NAT GW {nat_gw_id} is available.")

    # 6. Network Firewall rule group (domain allowlist)
    rec = have("rule_group")
    if rec:
        rule_group_arn = rec["arn"]
        log(f"[resume] rule group {rule_group_arn} already created.")
    else:
        name = f"microvm-egress-allowlist-{run_id}"
        resp = nf.create_rule_group(
            RuleGroupName=name, Type="STATEFUL", Capacity=RULE_GROUP_CAPACITY,
            RuleGroup={"RulesSource": {"RulesSourceList": {
                "Targets": args.allowlist,
                "TargetTypes": ["TLS_SNI", "HTTP_HOST"],
                "GeneratedRulesType": "ALLOWLIST",
            }}},
            Tags=tags_for(run_id, name),
        )
        rule_group_arn = resp["RuleGroupResponse"]["RuleGroupArn"]
        _append_resource(run_id, {"type": "rule_group", "arn": rule_group_arn})
        log(f"Created rule group {rule_group_arn} — polling for ACTIVE...")
        poll_until(
            lambda: nf.describe_rule_group(RuleGroupArn=rule_group_arn)["RuleGroupResponse"],
            lambda rg: rg["RuleGroupStatus"] == "ACTIVE",
            timeout=120, interval=10, label="rule group",
        )
        log(f"Rule group {rule_group_arn} is ACTIVE.")

    # 7. Firewall policy
    # StatelessDefaultActions/StatelessFragmentDefaultActions = aws:forward_to_sfe
    # on BOTH is required (not an arbitrary choice) — it is what sends every
    # stateless-pass-through packet into the stateful engine, which is the engine
    # that actually evaluates the domain allowlist from step 6. Any other value
    # here would let traffic bypass the allowlist check entirely. HOME_NET is left
    # at its default (the firewall's own VPC CIDR) — this is a single-VPC
    # architecture (no cross-VPC/Transit-Gateway path), and HOME_NET only matters
    # for cross-VPC stateful-rule matching, so no override is needed.
    rec = have("firewall_policy")
    if rec:
        policy_arn = rec["arn"]
        log(f"[resume] firewall policy {policy_arn} already created.")
    else:
        name = f"microvm-egress-policy-{run_id}"
        resp = nf.create_firewall_policy(
            FirewallPolicyName=name,
            FirewallPolicy={
                "StatelessDefaultActions": ["aws:forward_to_sfe"],
                "StatelessFragmentDefaultActions": ["aws:forward_to_sfe"],
                "StatefulRuleGroupReferences": [{"ResourceArn": rule_group_arn}],
            },
            Tags=tags_for(run_id, name),
        )
        policy_arn = resp["FirewallPolicyResponse"]["FirewallPolicyArn"]
        _append_resource(run_id, {"type": "firewall_policy", "arn": policy_arn})
        log(f"Created firewall policy {policy_arn} — polling for ACTIVE...")
        poll_until(
            lambda: nf.describe_firewall_policy(FirewallPolicyArn=policy_arn)["FirewallPolicyResponse"],
            lambda p: p["FirewallPolicyStatus"] == "ACTIVE",
            timeout=120, interval=10, label="firewall policy",
        )
        log(f"Firewall policy {policy_arn} is ACTIVE.")

    # 8. Network Firewall (async, up to ~10 min)
    rec = have("firewall")
    if rec:
        firewall_arn = rec["arn"]
        log(f"[resume] firewall {firewall_arn} already created.")
    else:
        name = f"microvm-egress-firewall-{run_id}"
        resp = nf.create_firewall(
            FirewallName=name, VpcId=vpc_id,
            SubnetMappings=[{"SubnetId": subnet_ids["firewall"]}],
            FirewallPolicyArn=policy_arn,
            Tags=tags_for(run_id, name),
        )
        firewall_arn = resp["Firewall"]["FirewallArn"]
        _append_resource(run_id, {"type": "firewall", "arn": firewall_arn})
        log(f"Created firewall {firewall_arn} — polling for READY (timeout 10 min)...")
        poll_until(
            lambda: nf.describe_firewall(FirewallArn=firewall_arn)["FirewallStatus"],
            lambda fs: fs.get("Status") == "READY",
            timeout=600, interval=15, label="firewall",
        )
        log(f"Firewall {firewall_arn} is READY.")

    # 8b. Network Firewall logging (ALERT + FLOW to CloudWatch Logs) — see
    # investigate-l7-egress-block/report.md recommendation #3. Without this, a
    # run has no firewall-side record at all and a client-side timeout can only
    # ever be *inferred* to be a block, never confirmed. Each LogType needs its
    # own LogDestinationConfig entry (AWS constraint) — both point at the same
    # log group here, which is allowed.
    log_group_name = f"/microvm-egress-test/{run_id}"
    rec = have("log_group", name=log_group_name)
    if rec:
        log(f"[resume] CloudWatch log group {log_group_name} already created.")
    else:
        try:
            logs_client.create_log_group(logGroupName=log_group_name)
        except ClientError as e:
            if "ResourceAlreadyExistsException" not in str(e):
                raise
        _append_resource(run_id, {"type": "log_group", "name": log_group_name})
        log(f"Created CloudWatch log group {log_group_name}")

    logging_rec = have("firewall_logging_configuration", firewall_arn=firewall_arn)
    if logging_rec:
        log(f"[resume] firewall logging configuration for {firewall_arn} already enabled.")
    else:
        # AWS's UpdateLoggingConfiguration only accepts ONE added/modified
        # LogDestinationConfig relative to the current state per call —
        # passing both ALERT and FLOW in a single call is rejected with
        # InvalidRequestException ("attempts to create/modify multiple log
        # destination configs"). So this is built up cumulatively: first a
        # call enabling just ALERT, then a second call with ALERT+FLOW
        # together (which only adds FLOW relative to the state after call 1).
        alert_config = {
            "LogType": "ALERT",
            "LogDestinationType": "CloudWatchLogs",
            "LogDestination": {"logGroup": log_group_name},
        }
        flow_config = {
            "LogType": "FLOW",
            "LogDestinationType": "CloudWatchLogs",
            "LogDestination": {"logGroup": log_group_name},
        }
        nf.update_logging_configuration(
            FirewallArn=firewall_arn,
            LoggingConfiguration={"LogDestinationConfigs": [alert_config]},
        )
        nf.update_logging_configuration(
            FirewallArn=firewall_arn,
            LoggingConfiguration={"LogDestinationConfigs": [alert_config, flow_config]},
        )
        # The logging configuration itself is deleted implicitly when the
        # firewall is deleted (it is not a separately-deletable AWS resource),
        # but it's still recorded here so a human reading the state file can
        # see logging was enabled for this run. The CloudWatch log group IS a
        # separate resource (recorded above as "log_group") and Teardown MUST
        # delete it explicitly.
        _append_resource(run_id, {"type": "firewall_logging_configuration", "firewall_arn": firewall_arn, "log_group": log_group_name})
        log(f"Enabled ALERT+FLOW logging on firewall {firewall_arn} -> log group {log_group_name}")

    # 9. Discover the firewall's VPC endpoint id for this AZ
    fw_status = nf.describe_firewall(FirewallArn=firewall_arn)["FirewallStatus"]
    sync_states = fw_status.get("SyncStates", {})
    endpoint_id = None
    for az, info in sync_states.items():
        endpoint_id = info.get("Attachment", {}).get("EndpointId")
        if endpoint_id:
            break
    if not endpoint_id:
        raise RuntimeError("Could not discover the firewall's VPC endpoint id from describe_firewall")
    log(f"Firewall VPC endpoint: {endpoint_id}")

    # 10. Route tables + routes
    # This is the standard AWS Network Firewall 3-subnet reference pattern
    # (private/firewall/public), confirmed against AWS's own
    # "Set up Network Firewall with a NAT gateway" guidance
    # (https://repost.aws/knowledge-center/network-firewall-set-up-with-nat-gateway)
    # and the distributed (igw+ngw) deployment model
    # (https://docs.aws.amazon.com/network-firewall/latest/developerguide/arch-igw-ngw.html):
    #   private subnet  -> 0.0.0.0/0 -> firewall VPC endpoint
    #   firewall subnet -> 0.0.0.0/0 -> NAT Gateway (so firewall-forwarded egress
    #                       traffic actually reaches the internet)
    #   public subnet   -> 0.0.0.0/0 -> Internet Gateway, PLUS a specific route for
    #                       the private subnet's CIDR back to the firewall VPC
    #                       endpoint (so return traffic from the internet is routed
    #                       back through the firewall for symmetric inspection,
    #                       rather than straight to the NAT GW's subnet) — this is
    #                       the "symmetric routing" requirement AWS's own guidance
    #                       calls out explicitly.
    route_tables = {}
    for role, subnet_role in (("private", "private"), ("firewall", "firewall"), ("public", "public")):
        rec = have("route_table", role=role)
        if rec:
            route_tables[role] = rec["id"]
            log(f"[resume] {role} route table {rec['id']} already created.")
            continue
        resp = ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "route-table", "Tags": tags_for(run_id, f"microvm-egress-test-{role}-rt-{run_id}")}],
        )
        rtb_id = resp["RouteTable"]["RouteTableId"]
        ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_ids[subnet_role])
        route_tables[role] = rtb_id
        _append_resource(run_id, {"type": "route_table", "role": role, "id": rtb_id})
        log(f"Created {role} route table {rtb_id} (associated with {subnet_role} subnet)")

    # Private subnet -> firewall endpoint
    try:
        ec2.create_route(RouteTableId=route_tables["private"], DestinationCidrBlock="0.0.0.0/0", VpcEndpointId=endpoint_id)
    except ClientError as e:
        if "RouteAlreadyExists" not in str(e):
            raise
    # Public subnet -> IGW
    try:
        ec2.create_route(RouteTableId=route_tables["public"], DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    except ClientError as e:
        if "RouteAlreadyExists" not in str(e):
            raise
    # Public subnet -> firewall endpoint for the private subnet's CIDR (symmetric
    # return routing, per AWS's reference guidance above).
    try:
        ec2.create_route(RouteTableId=route_tables["public"], DestinationCidrBlock=PRIVATE_SUBNET_CIDR, VpcEndpointId=endpoint_id)
    except ClientError as e:
        if "RouteAlreadyExists" not in str(e):
            raise
    # Firewall subnet -> NAT GW
    try:
        ec2.create_route(RouteTableId=route_tables["firewall"], DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_gw_id)
    except ClientError as e:
        if "RouteAlreadyExists" not in str(e):
            raise
    log("Route tables wired (private->firewall, firewall->NAT, public->IGW + return route).")

    # 11. Security group for the connector's ENIs
    rec = have("security_group", role="connector")
    if rec:
        sg_id = rec["id"]
        log(f"[resume] security group {sg_id} already created.")
    else:
        resp = ec2.create_security_group(
            GroupName=f"microvm-egress-test-sg-{run_id}", Description="Egress-only SG for the VPC egress connectors ENIs",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": tags_for(run_id, f"microvm-egress-test-sg-{run_id}")}],
        )
        sg_id = resp["GroupId"]
        ec2.authorize_security_group_egress(GroupId=sg_id, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        ])
        _append_resource(run_id, {"type": "security_group", "role": "connector", "id": sg_id})
        log(f"Created security group {sg_id}")

    # 12. Operator IAM role for the network connector
    # Permission set per the live create-network-connector CLI reference
    # (https://docs.aws.amazon.com/cli/latest/reference/lambda-core/create-network-connector.html):
    # "This role must have permissions for ec2:CreateNetworkInterface,
    # ec2:DeleteNetworkInterface, and related describe operations."
    iam = _session_cache["iam"]
    rec = have("iam_role", role="connector_operator")
    if rec:
        role_arn = rec["arn"]
        role_name = rec["name"]
        log(f"[resume] operator IAM role {role_arn} already created.")
    else:
        role_name = f"microvm-egress-test-operator-{run_id}"
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
        }
        resp = iam.create_role(
            RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust_policy),
            Tags=tags_for(run_id, role_name),
        )
        role_arn = resp["Role"]["Arn"]
        _append_resource(run_id, {"type": "iam_role", "role": "connector_operator", "name": role_name, "arn": role_arn})
        log(f"Created operator IAM role {role_arn}")

        policy_name = f"microvm-egress-test-operator-policy-{run_id}"
        permissions = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "ec2:CreateNetworkInterface",
                        "ec2:DeleteNetworkInterface",
                        "ec2:DeleteNetworkInterfacePermission",
                        "ec2:DescribeNetworkInterfaces",
                        "ec2:DescribeSubnets",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeVpcs",
                        "ec2:AssignPrivateIpAddresses",
                        "ec2:UnassignPrivateIpAddresses",
                    ],
                    "Resource": "*",
                },
                # AWS's documented connector prerequisites also require
                # ec2:CreateTags on the operator role, scoped to network-interface
                # ARNs and conditioned on the connector service tagging its own
                # ENIs (see connector-dataplane-eni/report.md section "2. Is the
                # operator IAM role/policy sufficient?").
                {
                    "Effect": "Allow",
                    "Action": "ec2:CreateTags",
                    "Resource": "arn:aws:ec2:*:*:network-interface/*",
                    "Condition": {"StringEquals": {
                        "ec2:ManagedResourceOperator": "network-connectors.lambda.amazonaws.com"
                    }},
                },
            ],
        }
        iam.put_role_policy(RoleName=role_name, PolicyName=policy_name, PolicyDocument=json.dumps(permissions))
        _append_resource(run_id, {"type": "iam_policy", "role": "connector_operator", "name": policy_name, "arn": f"{role_arn}/policy/{policy_name}"})
        log(f"Attached inline policy {policy_name} to {role_name}")
        log("Waiting 10s for IAM role propagation before creating the connector...")
        time.sleep(10)

    # 13. VPC egress network connector (async, up to ~10-12 min)
    # Tagging: create-network-connector's live CLI reference DOES expose a
    # --tags (map) parameter at creation time, so Project/RunId are applied
    # here natively, same as every other resource — no name-only fallback is
    # needed for this resource at CREATE time (verified against
    # https://docs.aws.amazon.com/cli/latest/reference/lambda-core/create-network-connector.html,
    # which documents --tags as "A map of key-value pairs to associate with the
    # network connector for organization, cost allocation, or access control").
    # NOTE: the live `lambda-core` boto3 service model is PascalCase
    # (Name/Configuration/OperatorRole/Tags/Identifier), NOT camelCase like
    # lambda-microvms — confirmed directly against the installed botocore model
    # (round-4 review finding). Every call below uses the corrected casing.
    # Separately: GetNetworkConnector's response has NO tags field at all (also
    # confirmed against the live model), so creation-time tagging does not help
    # Teardown's safety check for this one resource type — see
    # NetworkEgressTeardown.py's network_connector branch for the documented
    # ARN-based fallback this forces.
    rec = have("network_connector")
    if rec:
        connector_arn = rec["arn"]
        log(f"[resume] network connector {connector_arn} already created.")
    else:
        name = f"microvm-egress-connector-{run_id}"
        resp = lam_core.create_network_connector(
            Name=name,
            Configuration={"VpcEgressConfiguration": {
                "SubnetIds": [subnet_ids["private"]],
                "SecurityGroupIds": [sg_id],
                "NetworkProtocol": "IPv4",
                "AssociatedComputeResourceTypes": ["MicroVm"],
            }},
            OperatorRole=role_arn,
            Tags={"Project": PROJECT_TAG, "RunId": run_id, "Name": name},
        )
        connector_arn = resp["Arn"]
        _append_resource(run_id, {"type": "network_connector", "arn": connector_arn})
        log(f"Created network connector {connector_arn} — polling for ACTIVE (timeout 12 min)...")
        # Field name/enum confirmed against the live API reference: `State` field,
        # enum PENDING|ACTIVE|INACTIVE|FAILED|DELETING|DELETE_FAILED.
        poll_until(
            lambda: lam_core.get_network_connector(Identifier=connector_arn),
            lambda c: c.get("State") == "ACTIVE",
            timeout=720, interval=15, label="network connector",
        )
        log(f"Network connector {connector_arn} is ACTIVE.")

    # NOTE: there is deliberately no pre-launch data-plane/ENI readiness poll
    # here. Lambda MicroVM VPC egress-connector ENIs are Hyperplane-style,
    # cross-account-managed ENIs that are never enumerable from this account via
    # ec2 describe_network_interfaces (direct lookup returns NotFound), at any
    # point in a healthy connector's lifecycle, regardless of filter or how long
    # you poll — see connector-dataplane-eni/report.md. The connector's
    # control-plane State == ACTIVE poll above (step 13) is the correct
    # pre-launch connector-health signal; the actual data-plane/forwarding
    # proof is run_smoke_test(), which sends a real allowlisted HTTPS request
    # through the egress path AFTER the MicroVM launches and already gates the
    # deny verdict via classify_deny() + firewall-side CloudWatch evidence. That
    # post-launch check is the sole data-plane signal by design.

    return {
        "vpc_id": vpc_id, "subnet_ids": subnet_ids, "nat_gw_id": nat_gw_id,
        "connector_arn": connector_arn, "sg_id": sg_id,
        "firewall_arn": firewall_arn, "log_group_name": log_group_name,
    }


_session_cache = {}


def resolve_image_arn(arch, mem):
    if not IMAGE_ARN_BASE:
        log("❌ IMAGE_ARN / MICROVM_IMAGE_ARN not set — source scripts/config.sh first.")
        sys.exit(2)
    return f"{IMAGE_ARN_BASE}-{arch}-{mem}"


def launch_microvm(lam_vm, run_id, connector_arn, args):
    state = _read_state()
    rec = already_created(state, run_id, "microvm")
    if rec:
        log(f"[resume] MicroVM {rec['id']} already launched.")
        return rec["id"], rec.get("endpoint")

    image_arn = resolve_image_arn(args.arch, args.memory)
    if not EXEC_ROLE_ARN:
        log("❌ EXEC_ROLE_ARN / MICROVM_EXEC_ROLE_ARN not set — refusing to launch a "
            "MicroVM with no execution role (it would have no AWS credentials).")
        sys.exit(2)

    # Only egress is redirected through the firewall for this test — ingress is
    # unaffected and uses the repo's standard HTTP_INGRESS connector (this is what
    # makes /health and /execute reachable at all). INTERNET_EGRESS is
    # deliberately absent from egressNetworkConnectors.
    params = {
        "imageIdentifier": image_arn,
        "ingressNetworkConnectors": [INGRESS_CONNECTOR],
        "egressNetworkConnectors": [connector_arn],
        "idlePolicy": {
            "autoResumeEnabled": True,
            "maxIdleDurationSeconds": max(args.test_duration_minutes * 60, 600),
            "suspendedDurationSeconds": max(args.test_duration_minutes * 60, 600),
        },
        "maximumDurationInSeconds": max(args.test_duration_minutes * 60 + 600, 1200),
        "runHookPayload": json.dumps({
            "notebook_name": "network-egress-test",
            "session_id": f"network-egress-test-{run_id}",
            "checkpoint_enabled": False,
            "persistence_mode": "none",
            "env_vars": {},
        }),
        "executionRoleArn": EXEC_ROLE_ARN,
    }
    resp = lam_vm.run_microvm(**params)
    vm_id = resp["microvmId"]
    endpoint = resp["endpoint"]
    _append_resource(run_id, {"type": "microvm", "id": vm_id, "endpoint": endpoint})
    log(f"Launched MicroVM {vm_id} (endpoint={endpoint})")

    log("Polling get_microvm for RUNNING (timeout 3 min)...")
    poll_until(
        lambda: lam_vm.get_microvm(microvmIdentifier=vm_id),
        lambda vm: vm.get("state") == "RUNNING",
        timeout=180, interval=5, label="MicroVM boot",
    )
    log(f"MicroVM {vm_id} is RUNNING.")
    return vm_id, endpoint


def get_auth_token(lam_vm, vm_id):
    """Mint a fresh auth token exactly as microvm_manager.get_auth_token() does
    (proxy/platform/microvm_manager.py:827-845) — no caching needed here since
    this script makes only a handful of calls per run."""
    resp = lam_vm.create_microvm_auth_token(
        microvmIdentifier=vm_id, expirationInMinutes=30, allowedPorts=[{"allPorts": {}}],
    )
    return resp["authToken"]["X-aws-proxy-auth"]


def wait_for_health(endpoint, token, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"https://{endpoint}/health", headers={"X-aws-proxy-auth": token}, timeout=10)
            if r.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(5)
    return False


def run_execute(endpoint, token, code, timeout=30):
    """POST /execute directly against the VM's own endpoint. See module docstring
    "EXECUTION CHANNEL" for the exact mechanism this replicates."""
    r = requests.post(
        f"https://{endpoint}/execute",
        headers={"X-aws-proxy-auth": token, "Content-Type": "application/json"},
        json={"code": code}, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


# allow_redirects=False: s3.amazonaws.com (the old default) 307-redirects to
# aws.amazon.com, a host NOT on the allowlist — see investigate-l7-egress-block
# /report.md section 1/5. Following that redirect would make a working firewall
# path still FAIL the allow case. Disabling redirects and asserting a direct
# 2xx means the allow case can only pass when the chosen domain serves content
# directly, which is a property of the domain choice (see --allow-domain
# default below), not something this snippet should paper over by following
# redirects to arbitrary hosts.
ALLOW_SNIPPET = """
import time, requests
t0 = time.time()
try:
    r = requests.get("https://{domain}", timeout=10, allow_redirects=False)
    ok = 200 <= r.status_code < 300
    print({{"ok": ok, "status": r.status_code, "elapsed_s": round(time.time()-t0, 2)}})
except Exception as e:
    print({{"ok": False, "error": f"{{type(e).__name__}}: {{e}}", "elapsed_s": round(time.time()-t0, 2)}})
"""


# Variant of ALLOW_SNIPPET for "reachability" cases (cases 2, 5): reaching the
# host is the point, so ANY HTTP response — including a 4xx such as S3's 400/403
# with no credentials — proves the firewall let the traffic through. ok=True on
# any status code; only a connection-level/transport error yields ok=False. A
# HEAD is used (no body needed) but falls back semantics are the same either way.
REACHABLE_SNIPPET = """
import time, requests
t0 = time.time()
try:
    r = requests.request("{method}", "https://{domain}", timeout=10, allow_redirects=False)
    print({{"ok": True, "status": r.status_code, "elapsed_s": round(time.time()-t0, 2)}})
except Exception as e:
    print({{"ok": False, "error": f"{{type(e).__name__}}: {{e}}", "elapsed_s": round(time.time()-t0, 2)}})
"""

# Redirect-trap snippet (case 7): follow redirects so an allowlisted host that
# 307-redirects to a NON-allowlisted host gets blocked at the REDIRECT TARGET's
# SNI. Records whether the initial host was reachable and the final/last status.
REDIRECT_SNIPPET = """
import time, requests
t0 = time.time()
try:
    r = requests.get("https://{domain}", timeout=10, allow_redirects=True)
    hops = [h.status_code for h in r.history]
    print({{"ok": True, "status": r.status_code, "redirect_hops": hops,
            "final_url": r.url, "elapsed_s": round(time.time()-t0, 2)}})
except Exception as e:
    print({{"ok": False, "error": f"{{type(e).__name__}}: {{e}}", "elapsed_s": round(time.time()-t0, 2)}})
"""

# Plain-HTTP (port 80) snippet (case 8): exercises HTTP_HOST matching rather than
# TLS_SNI. A real block surfaces in the ALERT log under event.http.hostname.
HTTP_SNIPPET = """
import time, requests
t0 = time.time()
try:
    r = requests.get("http://{domain}", timeout=10, allow_redirects=False)
    print({{"ok": True, "status": r.status_code, "elapsed_s": round(time.time()-t0, 2)}})
except Exception as e:
    print({{"ok": False, "error": f"{{type(e).__name__}}: {{e}}", "elapsed_s": round(time.time()-t0, 2)}})
"""

# Forged-SNI snippet (case 9): stdlib only (socket + ssl), no `requests`. Opens a
# TLS connection to connect_host:443 but sends server_hostname=forged_sni in the
# ClientHello, demonstrating that filtering keys on the SNI VALUE, not the dialed
# destination. Records whether the handshake completed or was blocked — the case
# runner compares this to the documented expectation (it must not fail the run
# when the documented limitation holds).
FORGED_SNI_SNIPPET = """
import time, socket, ssl
t0 = time.time()
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("{connect_host}", 443), timeout=10)
    try:
        s = ctx.wrap_socket(raw, server_hostname="{forged_sni}")
        s.close()
        print({{"ok": True, "handshake": "completed", "elapsed_s": round(time.time()-t0, 2)}})
    finally:
        raw.close()
except Exception as e:
    print({{"ok": False, "handshake": "blocked", "error": f"{{type(e).__name__}}: {{e}}", "elapsed_s": round(time.time()-t0, 2)}})
"""


def classify_result(output_text):
    """Parse the printed dict (repr) from the /execute output. Returns
    (verdict, detail) where verdict is one of PASS/FAIL."""
    try:
        result = eval(output_text.strip().splitlines()[-1], {"__builtins__": {}}, {"True": True, "False": False})
    except Exception as e:
        return None, f"could not parse output: {e!r} raw={output_text!r}"
    return result, None


def run_smoke_test(endpoint, token, args):
    """End-to-end connectivity smoke test (recommendation #1 in
    investigate-l7-egress-block/report.md): proves the egress path actually
    forwards traffic BEFORE the deny case is allowed to be trusted. Runs an
    allowlisted, non-redirecting request (--allow-domain) through the same
    /execute mechanism run_assertions() uses. Returns (ok, detail) — ok=False
    means the whole run must report INCONCLUSIVE, not a deny PASS, because a
    deny-case timeout is indistinguishable from a broken data path when this
    smoke test itself times out."""
    log(f"Smoke test: requesting https://{args.allow_domain} through the egress "
        f"path to confirm it forwards traffic at all (precondition for trusting "
        f"the deny case)...")
    code = ALLOW_SNIPPET.format(domain=args.allow_domain)
    resp = run_execute(endpoint, token, code)
    result, parse_err = classify_result(resp.get("output", ""))
    if parse_err or result is None:
        detail = {"reason": parse_err or "no result"}
        log(f"  smoke test: FAILED to parse result — {detail}")
        return False, detail
    if result.get("ok"):
        log(f"  smoke test: OK — egress path is forwarding traffic ({result})")
        return True, result
    log(f"  smoke test: FAILED — egress path did not forward traffic ({result})")
    return False, result


def query_firewall_tls_evidence(cw, firewall_name, availability_zone, window_start, window_end):
    """Query the stateful engine's TLSReceivedPackets / DroppedPackets +
    TLSDroppedPackets CloudWatch metrics (namespace AWS/NetworkFirewall) for the
    given firewall/AZ over [window_start, window_end]. Returns a dict with the
    summed datapoint values for each metric (0 if no datapoints were returned —
    these metrics only publish nonzero values per AWS's own metric reference, so
    "no datapoints" and "zero" are the same signal here).

    This is the independent, firewall-side signal recommendation #3 in
    investigate-l7-egress-block/report.md calls for: TLSReceivedPackets nonzero
    is proof the TLS ClientHello actually reached the firewall; a
    DroppedPackets/TLSDroppedPackets increment is proof it was dropped. Neither
    is a reliable signal on its own — see classify_deny() below.

    A transient CloudWatch API error (throttling, InternalFailure, etc.) from
    get_metric_statistics degrades to a zero/no-datapoints value for that
    metric rather than propagating — poll_until() would otherwise let any such
    exception escape immediately and abort the entire run (see classify_deny()'s
    "metric_query_error" handling below for how this is still distinguished from
    a confirmed zero)."""

    errored_metrics = []

    def _sum_metric(metric_name):
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/NetworkFirewall",
                MetricName=metric_name,
                Dimensions=[
                    {"Name": "FirewallName", "Value": firewall_name},
                    {"Name": "AvailabilityZone", "Value": availability_zone},
                    {"Name": "Engine", "Value": "Stateful"},
                ],
                StartTime=window_start, EndTime=window_end,
                Period=60, Statistics=["Sum"],
            )
        except ClientError as e:
            log(f"  ⚠ CloudWatch get_metric_statistics({metric_name}) failed: {e} "
                f"— treating as zero/no-datapoints for this metric, but flagging "
                f"the query itself as having errored (not a confirmed zero).")
            errored_metrics.append(metric_name)
            return 0
        return sum(dp.get("Sum", 0) for dp in resp.get("Datapoints", []))

    result = {
        "TLSReceivedPackets": _sum_metric("TLSReceivedPackets"),
        "DroppedPackets": _sum_metric("DroppedPackets"),
        "TLSDroppedPackets": _sum_metric("TLSDroppedPackets"),
    }
    if errored_metrics:
        result["metric_query_error"] = (
            f"CloudWatch metric query failed for: {', '.join(errored_metrics)}"
        )
    return result


def query_firewall_alert_log(logs_client, log_group_name, firewall_name, deny_domain,
                             window_start, window_end, match_field="sni"):
    """Read the Network Firewall ALERT log and look for a stateful-engine entry
    that proves THIS run blocked the deny domain. This is the AUTHORITATIVE,
    per-connection signal (far stronger than the aggregate CloudWatch metrics in
    query_firewall_tls_evidence): the ALERT log records the exact matched host and
    the verdict.action for each inspected flow.

    match_field selects which field the deny_domain is matched against:
      "sni"  (default) — match on event.tls.sni, the host sent in the TLS
              ClientHello SNI extension (how TLS_SNI rule matches surface).
      "host" — match on event.http.hostname, the HTTP Host header (how HTTP_HOST
              rule matches surface for plain-HTTP, port-80 traffic). AWS's
              Suricata-derived ALERT schema exposes the matched host under
              event.http.hostname for HTTP_HOST rule matches, mirroring the
              event.tls.sni field used for TLS_SNI matches. The drop/blocked
              check (verdict.action == "drop" or alert.action == "blocked") is
              identical for both field selections.

    Returns a dict:
      {"matched": True, "entry": <raw event dict>}  when an ALERT event is found with
          the matched field == deny_domain AND a drop/blocked action
          (event.verdict.action == "drop" or event.alert.action == "blocked");
      {"matched": False, ...}  when the log was readable but no such entry was found
          (deny may genuinely not have been blocked, or logs haven't published yet);
      {"error": "..."}  when the log could not be read (group/stream missing, API
          error) — caller treats this as "log signal unavailable", NOT as proof of
          anything, and falls back to the metric-based evidence.

    The "sni" branch's matching, return shape, and semantics are unchanged from the
    original single-purpose version, so no existing caller is affected.

    Why read logs at all: run 20261010-171557-d4a764 showed the deny was REALLY
    blocked (ALERT log: sni=example.com, 'not matching any TLS allowlisted FQDNs',
    verdict.action=drop) while the Stateful-engine TLSReceivedPackets metric read
    zero for the window — so a metrics-only gate returned a false INCONCLUSIVE. The
    log is the ground truth; see connector-dataplane-eni/CONFIRMED-l7-block-*.md."""
    alert_stream = f"/aws/network-firewall/alert/{firewall_name}"
    try:
        # ALERT log stream names are suffixed with an hour bucket
        # (<firewall>_<YYYY-MM-DD-HH>), so match by prefix rather than exact name.
        streams = logs_client.describe_log_streams(
            logGroupName=log_group_name,
            logStreamNamePrefix=f"/aws/network-firewall/alert/{firewall_name}",
        ).get("logStreams", [])
        if not streams:
            return {"matched": False, "note": f"no ALERT log stream yet under {alert_stream}"}
        matched_entry = None
        scanned = 0
        for s in streams:
            events = logs_client.get_log_events(
                logGroupName=log_group_name,
                logStreamName=s["logStreamName"],
                startTime=int(window_start * 1000),
                endTime=int(window_end * 1000),
                startFromHead=True,
            ).get("events", [])
            for ev in events:
                scanned += 1
                try:
                    msg = json.loads(ev["message"])
                except (ValueError, KeyError):
                    continue
                e = msg.get("event", {})
                if match_field == "host":
                    matched_value = (e.get("http") or {}).get("hostname")
                else:
                    matched_value = (e.get("tls") or {}).get("sni")
                verdict_action = (e.get("verdict") or {}).get("action")
                alert_action = (e.get("alert") or {}).get("action")
                if matched_value == deny_domain and (verdict_action == "drop" or alert_action == "blocked"):
                    matched_entry = e
                    break
            if matched_entry:
                break
        if matched_entry:
            return {"matched": True, "entry": matched_entry, "scanned": scanned}
        return {"matched": False, "scanned": scanned,
                "note": f"no ALERT entry with {match_field}=={deny_domain} and a drop/blocked action found"}
    except ClientError as e:
        return {"error": f"could not read ALERT log {log_group_name}: {e}"}


# ── ALERT-log-first fast path (Part 1: reorder the deny verdict) ──────────────

# How long/often the fast path polls the ALERT log before giving up and letting
# the deny verdict fall back to the long CloudWatch metric poll. Short and
# The ALERT log is the AUTHORITATIVE, PRIMARY signal, so this is the main wait —
# sized to the OBSERVED ALERT-log publication lag. Live runs showed the drop entry
# takes ~5 minutes to appear in CloudWatch Logs, so a 90s window always missed and
# fell through to the (useless, zero-reading) 300s metric poll — the worst of both.
# 360s/15s matches reality; the metric poll below is now only a short last-resort.
ALERT_LOG_FAST_TIMEOUT_SECONDS = 360
ALERT_LOG_FAST_INTERVAL_SECONDS = 15


def confirm_deny_via_alert_log_fast(logs_client, log_group_name, firewall_name, deny_domain,
                                    request_ts, match_field="sni",
                                    timeout=ALERT_LOG_FAST_TIMEOUT_SECONDS,
                                    interval=ALERT_LOG_FAST_INTERVAL_SECONDS):
    """Short, bounded poll of the ALERT log for a matching drop entry — the
    fast-path primary signal for every DENY-style case. Reuses
    query_firewall_alert_log() verbatim (no duplicated matching logic); only the
    WHEN (first, before the long metric poll) changes.

    On each iteration it recomputes window_end = now + 10 (same recompute-each-poll
    pattern run_assertions()'s _get_evidence() closure uses) so late-published
    events still land inside the window. Returns the LAST query result — a matched
    dict as soon as one is found, otherwise the final unmatched/`note` dict when the
    short timeout elapses (it never raises on timeout, mirroring the existing
    `except TimeoutError: firewall_evidence = _get_evidence()` fallback). Any
    unexpected failure degrades to {"error": str(e)} so the caller can fall back to
    metrics — a firewall-side query must never crash the run."""
    try:
        def _query():
            return query_firewall_alert_log(
                logs_client, log_group_name, firewall_name, deny_domain,
                window_start=request_ts - 10, window_end=time.time() + 10,
                match_field=match_field,
            )

        try:
            return poll_until(
                _query,
                lambda r: isinstance(r, dict) and r.get("matched"),
                timeout=timeout, interval=interval,
                label=f"ALERT-log fast path ({match_field}=={deny_domain})",
            )
        except TimeoutError:
            # No matching drop entry within the short window — return the last
            # (unmatched) result so the caller falls back to the metric poll.
            return _query()
    except Exception as e:  # noqa: BLE001 — firewall-side query must never crash the run
        return {"error": str(e)}


def classify_deny(deny_result, firewall_evidence, alert_log_evidence=None):
    """Gate the deny verdict on the client-side result PLUS firewall-side evidence.
    Returns (verdict, detail_dict). verdict is one of PASS/FAIL/INCONCLUSIVE.

    PRIMARY firewall-side signal = the ALERT LOG (alert_log_evidence): a log entry
    proving the deny domain's SNI was seen and dropped is authoritative, per-flow
    proof of an L7 block — this is a PASS regardless of what the aggregate metrics
    say. The CloudWatch METRICS (firewall_evidence) are a FALLBACK, used only when
    the log signal is unavailable. This ordering exists because metrics gave a false
    INCONCLUSIVE on a run the ALERT log proved was a real block
    (see query_firewall_alert_log docstring).

    Per investigate-l7-egress-block/report.md recommendation #3: a client-side
    timeout alone is not proof of a block — it is equally consistent with the
    packet never reaching the firewall at all (the prior run's actual root
    cause). PASS now additionally requires TLSReceivedPackets > 0 (the
    ClientHello reached the firewall) AND a DroppedPackets/TLSDroppedPackets
    increment (it was then dropped). If the client saw a connection-level error
    but TLSReceivedPackets stayed at zero, the correct verdict is INCONCLUSIVE:
    traffic never reached the firewall — NOT a PASS."""
    if deny_result is None:
        return "FAIL", {"reason": "no result"}
    if deny_result.get("ok"):
        return "FAIL", {"evidence": deny_result, "reason": "request succeeded; expected it to be blocked"}

    error_str = deny_result.get("error", "")
    if any(marker in error_str for marker in DNS_ERROR_MARKERS):
        # DNS resolution failure is explicitly NOT proof the firewall blocked
        # it (Network Firewall matches on TLS SNI / HTTP Host; DNS still
        # resolves even for a blocked domain) — classify as FAIL/inconclusive.
        return "FAIL", {"evidence": deny_result, "reason": "DNS resolution error — inconclusive, not proof of firewall blocking"}

    if any(marker in error_str for marker in DROP_TIMEOUT_ERRORS):
        deny_mechanism = "drop-timeout"
    elif any(marker in error_str for marker in REJECT_RESET_ERRORS):
        deny_mechanism = "reject-reset"
    else:
        return "FAIL", {"evidence": deny_result, "reason": "error was not a recognized connection-level failure"}

    detail = {"evidence": deny_result, "deny_mechanism": deny_mechanism,
              "firewall_evidence": firewall_evidence, "alert_log_evidence": alert_log_evidence}

    # PRIMARY signal: the ALERT log. A log entry proving the deny domain's SNI was
    # seen and dropped/blocked is authoritative per-flow proof of the L7 block —
    # PASS regardless of the aggregate metrics (which gave a false INCONCLUSIVE on
    # run 20261010-171557-d4a764 while the ALERT log showed a real drop).
    if alert_log_evidence and alert_log_evidence.get("matched"):
        entry = alert_log_evidence.get("entry", {})
        sig = (entry.get("alert") or {}).get("signature", "")
        detail["reason"] = (
            f"firewall-confirmed via ALERT log: tls.sni matched the deny domain and the "
            f"firewall recorded a drop/blocked action"
            + (f" (signature: {sig!r})" if sig else "")
        )
        return "PASS", detail

    # FALLBACK: aggregate CloudWatch metrics, used only when the log signal was not
    # available (not yet published, log unreadable, or no matching entry found).
    tls_received = firewall_evidence.get("TLSReceivedPackets", 0)
    dropped = firewall_evidence.get("DroppedPackets", 0) + firewall_evidence.get("TLSDroppedPackets", 0)

    # Note the ALERT-log outcome in the fallback reasons so a human can see the
    # primary signal was consulted and why it didn't conclude the verdict.
    log_note = ""
    if alert_log_evidence:
        if alert_log_evidence.get("error"):
            log_note = f" (ALERT log unavailable: {alert_log_evidence['error']})"
        elif not alert_log_evidence.get("matched"):
            log_note = f" (ALERT log read but no matching drop entry: {alert_log_evidence.get('note', '')})"

    if tls_received <= 0:
        if firewall_evidence.get("metric_query_error"):
            detail["reason"] = (
                f"firewall-side evidence unavailable: {firewall_evidence['metric_query_error']} "
                f"— cannot confirm whether traffic reached the firewall or not" + log_note
            )
        else:
            detail["reason"] = (
                "traffic never reached the firewall — TLSReceivedPackets was zero "
                "for the test window; this client-side error is indistinguishable "
                "from a broken data path (see investigate-l7-egress-block/report.md)" + log_note
            )
        return "INCONCLUSIVE", detail

    if dropped <= 0:
        detail["reason"] = (
            "TLSReceivedPackets was nonzero (packet reached the firewall) but no "
            "DroppedPackets/TLSDroppedPackets increment was observed — cannot "
            "confirm the firewall actually dropped it" + log_note
        )
        return "INCONCLUSIVE", detail

    detail["reason"] = ("firewall-confirmed via metrics (fallback): TLSReceivedPackets nonzero "
                        "and DroppedPackets/TLSDroppedPackets incremented" + log_note)
    return "PASS", detail


def evaluate_deny_case(logs_client, cw, firewall_name, availability_zone, log_group_name,
                       deny_domain, deny_result, request_ts, match_field="sni"):
    """ALERT-log-FIRST deny evaluation (Part 1). Decides WHEN each firewall-side
    query runs; it does NOT change classify_deny()'s verdict rules.

    Order:
      1. Consult the ALERT log FIRST via confirm_deny_via_alert_log_fast() (short
         ≈90s/10s poll). If it confirms a matching drop, call classify_deny() with
         an EMPTY firewall_evidence ({}) — classify_deny()'s primary-signal branch
         short-circuits on alert_log_evidence["matched"] before it ever reads
         firewall_evidence, so the long ~300s metric poll is SKIPPED entirely. This
         is the required efficiency fix (a real block reads zero metrics and would
         otherwise burn the full metric timeout).
      2. Otherwise (no match / log error), fall back to the EXISTING long metric
         poll (poll_until over query_firewall_tls_evidence with
         METRIC_POLL_TIMEOUT_SECONDS/METRIC_POLL_INTERVAL_SECONDS), then call
         classify_deny() with the real metrics and the (unmatched) fast-path log
         evidence — identical to the pre-reorder call shape.

    Returns (verdict, detail) exactly like classify_deny(); detail additionally
    carries "deny_check_path" ("alert_log_fast" | "metrics_fallback") so the
    per-case result table can show which path confirmed each deny case."""
    window_start = request_ts - METRIC_WINDOW_PADDING_SECONDS

    log(f"  reading firewall ALERT log FIRST for a dropped {match_field} entry matching "
        f"{deny_domain} (authoritative per-flow signal; up to "
        f"{ALERT_LOG_FAST_TIMEOUT_SECONDS}s, every {ALERT_LOG_FAST_INTERVAL_SECONDS}s)...")
    alert_log_evidence = confirm_deny_via_alert_log_fast(
        logs_client, log_group_name, firewall_name, deny_domain,
        request_ts=request_ts, match_field=match_field,
    )

    if isinstance(alert_log_evidence, dict) and alert_log_evidence.get("matched"):
        # FAST PATH: ALERT log confirmed the drop — skip the long metric poll
        # entirely. classify_deny()'s primary branch short-circuits on the matched
        # log evidence before reading firewall_evidence, so {} is safe here.
        log("  ALERT log confirmed the drop on the fast path — skipping the long "
            "CloudWatch metric poll.")
        verdict, detail = classify_deny(deny_result, {}, alert_log_evidence)
        detail["deny_check_path"] = "alert_log_fast"
        return verdict, detail

    # FALLBACK: no fast-path confirmation — run the existing long metric poll.
    log(f"  ALERT log did not confirm within the short window — falling back to the "
        f"CloudWatch metric poll (up to {METRIC_POLL_TIMEOUT_SECONDS}s, every "
        f"{METRIC_POLL_INTERVAL_SECONDS}s to absorb metric publication lag)...")

    def _get_evidence():
        window_end = time.time() + METRIC_WINDOW_PADDING_SECONDS
        return query_firewall_tls_evidence(
            cw, firewall_name, availability_zone,
            window_start=window_start, window_end=window_end,
        )

    try:
        firewall_evidence = poll_until(
            _get_evidence,
            lambda ev: ev.get("TLSReceivedPackets", 0) > 0,
            timeout=METRIC_POLL_TIMEOUT_SECONDS, interval=METRIC_POLL_INTERVAL_SECONDS,
            label="firewall TLS evidence",
        )
    except TimeoutError:
        # No TLSReceivedPackets ever showed up within the poll window — use
        # whatever the last query returned (almost certainly all-zero) so
        # classify_deny() can still produce its evidence-based verdict
        # instead of raising.
        firewall_evidence = _get_evidence()

    verdict, detail = classify_deny(deny_result, firewall_evidence, alert_log_evidence)
    detail["deny_check_path"] = "metrics_fallback"
    return verdict, detail


# ── Declarative case matrix (Part 3) ─────────────────────────────────────────

def build_case_list(args):
    """Build the ordered, declarative CASE LIST the matrix runner iterates. Built
    from args (not a static constant) because several cases depend on
    --allow-domain / --deny-test-domains overrides. Returns a list of dicts, one
    per case, each shaped:
        {name, description, kind, target, expected, params}
    `expected` is one of: "allowed", "blocked", "inconclusive_by_design",
    "allowed_then_redirect_blocked", "known_limitation".
    Case 9 (forged SNI) is omitted when --skip-forged-sni is passed."""
    deny_domains = args.deny_test_domains
    deny1 = deny_domains[0]
    deny2 = deny_domains[1] if len(deny_domains) > 1 else deny_domains[0]

    cases = [
        {
            "name": "allow_pypi_apex",
            "description": f"allow apex {args.allow_domain} (HTTPS, no redirects) -> ALLOWED (2xx)",
            "kind": "allow_probe",
            "target": args.allow_domain,
            "expected": "allowed",
            "params": {},
        },
        {
            "name": "allow_api_github",
            "description": "allow api.github.com (HTTPS) -> ALLOWED (reaching host is the point)",
            "kind": "allow_probe_reachable",
            "target": "api.github.com",
            "expected": "allowed",
            "params": {"method": "GET"},
        },
        {
            "name": "deny_example_com",
            "description": f"deny {deny1} (HTTPS) -> BLOCKED, confirmed via ALERT log (sni + drop)",
            "kind": "deny_probe",
            "target": deny1,
            "expected": "blocked",
            "params": {},
        },
        {
            "name": "deny_example_org",
            "description": f"deny {deny2} (HTTPS) -> BLOCKED, confirmed via ALERT log",
            "kind": "deny_probe",
            "target": deny2,
            "expected": "blocked",
            "params": {},
        },
        {
            "name": "allow_subdomain_suffix",
            "description": "subdomain of .amazonaws.com suffix target -> ALLOWED "
                           "(403/400 from S3 still proves the firewall let it through)",
            "kind": "allow_probe_reachable",
            "target": "s3.us-west-2.amazonaws.com",
            "expected": "allowed",
            "params": {"method": "HEAD"},
        },
        {
            "name": "deny_lookalike_suffix",
            # Suffix-confusion target: 'amazonaws.com.badssl.com' CONTAINS the
            # allowlisted string 'amazonaws.com' but is NOT a subdomain of it (it's a
            # subdomain of badssl.com), so a correct '.amazonaws.com' suffix match must
            # NOT over-match it → BLOCKED. Crucially it RESOLVES (badssl.com serves
            # wildcard DNS), so the TLS ClientHello actually reaches the firewall and
            # the SNI is inspected. The previous target 'notamazonaws.com' is an
            # UNREGISTERED domain that fails at DNS resolution before the firewall ever
            # sees it, which (correctly) classifies as "DNS error, not a block" and
            # made this case a false FAIL — the request never reached the firewall.
            "description": "look-alike host (amazonaws.com.badssl.com — resolves, but is "
                           "NOT a subdomain of amazonaws.com) -> BLOCKED "
                           "(proves .amazonaws.com suffix match does not over-match)",
            "kind": "deny_probe",
            "target": "amazonaws.com.badssl.com",
            "expected": "blocked",
            "params": {},
        },
        {
            "name": "allow_redirect_to_denied",
            "description": "allowlisted s3.amazonaws.com 307-redirects to NON-allowlisted "
                           "aws.amazon.com -> redirect target BLOCKED (why allow-domain choice matters)",
            "kind": "redirect_trap",
            "target": "s3.amazonaws.com",
            "expected": "allowed_then_redirect_blocked",
            "params": {"redirect_target": "aws.amazon.com"},
        },
        {
            "name": "deny_http_host",
            "description": f"plain HTTP (port 80) to {deny1} -> BLOCKED via HTTP_HOST matching "
                           "(ALERT log app_proto http, Host header)",
            "kind": "deny_probe_http",
            "target": deny1,
            "expected": "blocked",
            "params": {},
        },
        {
            "name": "forged_sni_known_limitation",
            "description": "forged SNI: dial an allowed host but send a denied SNI "
                           "(documented bypass boundary) -> KNOWN-LIMITATION (does not fail the run)",
            "kind": "forged_sni_probe",
            "target": deny1,
            "expected": "known_limitation",
            "params": {
                # Dial a reachable allowed destination, but present a denied SNI in
                # the ClientHello. The documented limitation is that the match keys
                # on the SNI value, so a forged (denied) SNI toward an allowed
                # destination is expected to be BLOCKED (handshake fails). This case
                # SURFACES and records the behavior; it stays green as long as the
                # documented expectation holds.
                "connect_host": args.allow_domain,
                "forged_sni": deny1,
                "documented_bypass_expected": False,
            },
        },
        {
            "name": "dns_failure_not_a_block",
            "description": "NXDOMAIN host (.invalid) -> INCONCLUSIVE/FAIL, NOT a firewall block "
                           "(proves classify_deny()'s DNS special-case)",
            "kind": "dns_failure_probe",
            "target": f"this-host-does-not-exist-{uuid.uuid4().hex[:8]}.invalid",
            "expected": "inconclusive_by_design",
            "params": {},
        },
    ]

    if args.skip_forged_sni:
        cases = [c for c in cases if c["name"] != "forged_sni_known_limitation"]
    return cases


def _expectation_met(case, verdict):
    """Does a case's actual verdict meet its declared `expected` outcome?
    This is the single source of truth for the aggregate run verdict."""
    expected = case["expected"]
    if expected == "allowed":
        return verdict == "PASS"
    if expected in ("blocked", "allowed_then_redirect_blocked"):
        return verdict == "PASS"
    if expected == "inconclusive_by_design":
        # The DNS case must NOT be a blocked PASS; FAIL or INCONCLUSIVE both mean
        # "correctly not treated as a firewall block".
        return verdict in ("FAIL", "INCONCLUSIVE")
    if expected == "known_limitation":
        # The forged-SNI case stays green as long as the documented behavior held
        # (run_case maps that to verdict KNOWN_LIMITATION).
        return verdict == "KNOWN_LIMITATION"
    return False


def _case_record(case, verdict, reason, evidence=None, alert_log_evidence=None,
                 metric_evidence=None):
    return {
        "name": case["name"],
        "description": case["description"],
        "target": case["target"],
        "expected": case["expected"],
        "verdict": verdict,
        "evidence": evidence,
        "alert_log_evidence": alert_log_evidence,
        "metric_evidence": metric_evidence,
        "reason": reason,
    }


def run_case(case, ctx):
    """Execute one case and return its per-case record dict. Dispatches on
    case["kind"]. Reuses the existing helpers (run_execute, classify_result,
    query_firewall_tls_evidence, query_firewall_alert_log, evaluate_deny_case) and
    the module snippets — no duplicated matching logic. Every firewall-side
    evidence query is wrapped so no case can raise out of run_case: a CloudWatch
    /logs failure degrades the case to INCONCLUSIVE-with-reason."""
    endpoint = ctx["endpoint"]
    token = ctx["token"]
    cw = ctx["cw"]
    logs_client = ctx["logs_client"]
    firewall_name = ctx["firewall_name"]
    availability_zone = ctx["availability_zone"]
    log_group_name = ctx["log_group_name"]
    kind = case["kind"]
    target = case["target"]

    log(f"[case {case['name']}] {case['description']}")

    # ── allow-style probes ────────────────────────────────────────────────
    if kind in ("allow_probe", "allow_probe_reachable"):
        if kind == "allow_probe":
            code = ALLOW_SNIPPET.format(domain=target)
        else:
            code = REACHABLE_SNIPPET.format(domain=target, method=case["params"].get("method", "GET"))
        request_ts = time.time()
        resp = run_execute(endpoint, token, code)
        result, parse_err = classify_result(resp.get("output", ""))
        if parse_err or result is None:
            return _case_record(case, "FAIL", parse_err or "no result")
        if result.get("ok"):
            return _case_record(case, "PASS", "reached the allowed host", evidence=result)

        # FAIL on the client side: distinguish "firewall blocked it" (real FAIL)
        # from a transient network error (INCONCLUSIVE) via the ALERT log + metrics,
        # exactly as the old allow-case FAIL branch did. Wrapped so it can't crash.
        alert_ev = None
        metric_ev = None
        try:
            alert_ev = query_firewall_alert_log(
                logs_client, log_group_name, firewall_name, target,
                window_start=request_ts - METRIC_WINDOW_PADDING_SECONDS,
                window_end=time.time() + 10, match_field="sni",
            )
        except Exception as e:  # noqa: BLE001 — evidence query must not crash the case
            alert_ev = {"error": str(e)}
        try:
            metric_ev = query_firewall_tls_evidence(
                cw, firewall_name, availability_zone,
                window_start=request_ts - METRIC_WINDOW_PADDING_SECONDS,
                window_end=time.time() + METRIC_WINDOW_PADDING_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 — evidence query must not crash the case
            metric_ev = {"error": str(e)}

        if isinstance(alert_ev, dict) and alert_ev.get("matched"):
            reason = "allowed host was BLOCKED by the firewall (ALERT log shows a drop) — real FAIL"
            return _case_record(case, "FAIL", reason, evidence=result,
                                 alert_log_evidence=alert_ev, metric_evidence=metric_ev)
        reason = ("allow probe failed but no firewall drop was found — transient/connection "
                  "error, not a firewall block (INCONCLUSIVE, not a FAIL)")
        return _case_record(case, "INCONCLUSIVE", reason, evidence=result,
                             alert_log_evidence=alert_ev, metric_evidence=metric_ev)

    # ── deny-style probes (ALERT-log-first) ───────────────────────────────
    if kind in ("deny_probe", "deny_probe_http"):
        if kind == "deny_probe":
            code = ALLOW_SNIPPET.format(domain=target)
            match_field = "sni"
        else:
            code = HTTP_SNIPPET.format(domain=target)
            match_field = "host"
        request_ts = time.time()
        resp = run_execute(endpoint, token, code)
        result, parse_err = classify_result(resp.get("output", ""))
        if parse_err or result is None:
            return _case_record(case, "FAIL", parse_err or "no result")
        verdict, detail = evaluate_deny_case(
            logs_client, cw, firewall_name, availability_zone, log_group_name,
            target, result, request_ts, match_field=match_field,
        )
        return _case_record(
            case, verdict, detail.get("reason", ""), evidence=result,
            alert_log_evidence=detail.get("alert_log_evidence"),
            metric_evidence=detail.get("firewall_evidence"),
        )

    # ── redirect trap ─────────────────────────────────────────────────────
    if kind == "redirect_trap":
        redirect_target = case["params"]["redirect_target"]
        code = REDIRECT_SNIPPET.format(domain=target)
        request_ts = time.time()
        resp = run_execute(endpoint, token, code)
        result, parse_err = classify_result(resp.get("output", ""))
        if parse_err or result is None:
            return _case_record(case, "FAIL", parse_err or "no result")
        # The followed redirect to a NON-allowlisted host should be blocked at the
        # REDIRECT TARGET's SNI. Confirm via the ALERT log for aws.amazon.com (not
        # the original host). The client either errored following the redirect
        # (ok=False) or the firewall dropped the redirect-target connection.
        alert_ev = None
        try:
            alert_ev = confirm_deny_via_alert_log_fast(
                logs_client, log_group_name, firewall_name, redirect_target,
                request_ts=request_ts, match_field="sni",
            )
        except Exception as e:  # noqa: BLE001 — evidence query must not crash the case
            alert_ev = {"error": str(e)}
        if isinstance(alert_ev, dict) and alert_ev.get("matched"):
            reason = (f"initial host reachable; followed redirect to {redirect_target} was BLOCKED "
                      f"(ALERT log drop for the REDIRECT TARGET's SNI, not the original) — "
                      f"why allow-domain choice matters")
            return _case_record(case, "PASS", reason, evidence=result, alert_log_evidence=alert_ev)
        reason = (f"could not confirm a firewall drop for the redirect target {redirect_target} "
                  f"within the short window — INCONCLUSIVE")
        return _case_record(case, "INCONCLUSIVE", reason, evidence=result, alert_log_evidence=alert_ev)

    # ── forged SNI (documented known-limitation) ──────────────────────────
    if kind == "forged_sni_probe":
        params = case["params"]
        code = FORGED_SNI_SNIPPET.format(
            connect_host=params["connect_host"], forged_sni=params["forged_sni"],
        )
        resp = run_execute(endpoint, token, code)
        result, parse_err = classify_result(resp.get("output", ""))
        if parse_err or result is None:
            return _case_record(case, "INCONCLUSIVE", parse_err or "no result (forged-SNI probe)")
        # documented_bypass_expected: did we expect the handshake to COMPLETE
        # (bypass) or be BLOCKED? The firewall keys on the SNI VALUE, so a denied
        # forged SNI toward an allowed destination is expected BLOCKED (bypass
        # NOT expected here). As long as the observed behavior matches the
        # documented expectation, the case is a green KNOWN_LIMITATION; a result
        # that contradicts the documented expectation flips it to FAIL.
        bypass_expected = params.get("documented_bypass_expected", False)
        handshake_completed = bool(result.get("ok"))
        if handshake_completed == bypass_expected:
            reason = (f"forged-SNI behavior matched the documented expectation "
                      f"(handshake {'completed' if handshake_completed else 'blocked'}; "
                      f"documented bypass_expected={bypass_expected}) — recorded as a "
                      f"known limitation, does not fail the run")
            return _case_record(case, "KNOWN_LIMITATION", reason, evidence=result)
        reason = (f"forged-SNI behavior CONTRADICTED the documented expectation "
                  f"(handshake {'completed' if handshake_completed else 'blocked'}; "
                  f"documented bypass_expected={bypass_expected}) — FAIL")
        return _case_record(case, "FAIL", reason, evidence=result)

    # ── DNS/NXDOMAIN failure is NOT a firewall block ──────────────────────
    if kind == "dns_failure_probe":
        code = ALLOW_SNIPPET.format(domain=target)
        resp = run_execute(endpoint, token, code)
        result, parse_err = classify_result(resp.get("output", ""))
        if parse_err or result is None:
            return _case_record(case, "INCONCLUSIVE", parse_err or "no result (DNS probe)")
        # Feed the client error through classify_deny() with NO firewall evidence
        # (empty metrics, no log match). classify_deny() special-cases the DNS
        # markers and returns FAIL ("not proof of a firewall block") — it must
        # NEVER return a blocked PASS here. This proves the existing DNS branch.
        verdict, detail = classify_deny(result, {}, {"matched": False, "note": "DNS case: no log consulted"})
        reason = detail.get("reason", "")
        if verdict == "PASS":
            # Would mean classify_deny() wrongly treated an NXDOMAIN as a block.
            return _case_record(case, "FAIL",
                                "DNS/NXDOMAIN failure was wrongly classified as a firewall block",
                                evidence=result)
        return _case_record(case, verdict, reason, evidence=result)

    return _case_record(case, "INCONCLUSIVE", f"unknown case kind: {kind}")


def run_case_matrix(ctx, cases):
    """Iterate the (already-filtered) case list, run each via run_case(), and
    return the list of per-case records in order."""
    records = []
    for case in cases:
        rec = run_case(case, ctx)
        met = _expectation_met(case, rec["verdict"])
        log(f"  -> {rec['name']}: {rec['verdict']} "
            f"({'meets' if met else 'MISSES'} expected={rec['expected']}) — {rec['reason']}")
        records.append(rec)
    return records


# ── Cost estimate + confirm gate ────────────────────────────────────────────

def print_cost_estimate(duration_minutes):
    firewall_hourly = 0.395
    nat_hourly = 0.045  # typically waived in this co-located topology, see NETWORK_EGRESS.md
    hours = duration_minutes / 60.0
    est_firewall = firewall_hourly * hours
    print("")
    print("Estimated cost for this test run (rough, rate-based — NOT a live Cost Explorer query):")
    print(f"  Network Firewall endpoint : ${firewall_hourly:.3f}/hr  x {hours:.2f}h  ~= ${est_firewall:.2f}")
    print(f"  NAT Gateway               : ${nat_hourly:.3f}/hr (typically $0 in this topology per NETWORK_EGRESS.md)")
    print(f"  MicroVM compute           : negligible for a short test run")
    print(f"  --> approximately ${est_firewall:.2f} for a {duration_minutes}-minute test, PLUS any extra time")
    print(f"      the infrastructure keeps running until you run NetworkEgressTeardown.py.")
    print("  Remember to run NetworkEgressTeardown.py when you are done!")
    print("")


def confirm(prompt, auto_yes):
    if auto_yes:
        return True
    resp = input(f"{prompt} Type 'yes' to proceed: ")
    return resp.strip().lower() == "yes"


# ── CLI ──────────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser(
        description="Provision AWS Network Firewall egress filtering end-to-end, "
                     "launch a MicroVM wired to it, and test allow/deny domain filtering.",
    )
    ap.add_argument("--run-id", help="Resume or name a specific run id (see Idempotency rules in design.md)")
    ap.add_argument("--yes", "-y", action="store_true", help="Skip the interactive confirmation prompt")
    ap.add_argument("--allowlist", default=",".join(DEFAULT_ALLOWLIST),
                     help=f"Comma-separated allowlist domains (default: {','.join(DEFAULT_ALLOWLIST)})")
    ap.add_argument("--deny-test-domain", default=None,
                     help="Single domain expected to be blocked. If given, OVERRIDES "
                          "--deny-test-domains entirely (used as its sole entry) — kept for "
                          "backward compatibility. Default: unset (falls back to "
                          f"--deny-test-domains: {','.join(DEFAULT_DENY_DOMAINS)})")
    ap.add_argument("--deny-test-domains", default=",".join(DEFAULT_DENY_DOMAINS),
                     help=f"Comma-separated domains expected to be blocked (default: "
                          f"{','.join(DEFAULT_DENY_DOMAINS)})")
    ap.add_argument("--allow-domain", default="pypi.org",
                     help="Domain used for the allow-case probe and the post-health smoke test "
                          "(default: pypi.org — on the allowlist and does not 307-redirect to a "
                          "non-allowlisted host, unlike the old s3.amazonaws.com default; see "
                          "investigate-l7-egress-block/report.md section 1/5)")
    ap.add_argument("--cases", default="all",
                     help="Comma-separated case names to run, or 'all' (default) to run the "
                          "full matrix. Unknown names error out listing the valid case names.")
    ap.add_argument("--skip-forged-sni", action="store_true",
                     help="Skip the forged-SNI known-limitation case (case 9) — useful if the "
                          "environment can't support raw sockets. Default: False (runs by "
                          "default so a bare --yes run executes the full matrix).")
    ap.add_argument("--az", default=None,
                     help="Availability zone (NAME, e.g. us-west-2a) to place all subnets in. "
                          "Default: auto-pick an available AZ that is not in the known "
                          "MicroVm-unsupported set (usw2-az4). Override if AWS's MicroVm AZ "
                          "offering differs in your region.")
    # TEST_ARCH (suite-wide opt-in) wins over DEFAULT_IMAGE_ARCH, which config.sh always sets.
    ap.add_argument("--arch", default=os.environ.get("TEST_ARCH") or os.environ.get("DEFAULT_IMAGE_ARCH", "arm64"),
                    help="MicroVM CPU architecture (default $TEST_ARCH, else $DEFAULT_IMAGE_ARCH, else arm64)")
    ap.add_argument("--memory", type=int, default=2048, help="MicroVM memory tier (MiB)")
    ap.add_argument("--test-duration-minutes", type=int, default=30, help="Used only for the cost estimate and VM idle/max-duration sizing")
    args = ap.parse_args()

    args.allowlist = [d.strip() for d in args.allowlist.split(",") if d.strip()]

    if args.deny_test_domain:
        log(f"Note: --deny-test-domain '{args.deny_test_domain}' was explicitly passed — it "
            f"overrides --deny-test-domains and is used as the sole deny-test domain.")
        args.deny_test_domains = [args.deny_test_domain]
    else:
        args.deny_test_domains = [d.strip() for d in args.deny_test_domains.split(",") if d.strip()]
        args.deny_test_domain = args.deny_test_domains[0]

    colliding = [d for d in args.deny_test_domains if d in args.allowlist]
    if colliding:
        log(f"❌ --deny-test-domains {colliding} collides with the allowlist "
            f"{args.allowlist} — a deny-case test against an allowed domain would silently "
            f"produce a false PASS. Choose different deny-test domains.")
        sys.exit(2)
    if args.test_duration_minutes <= 0:
        log("❌ --test-duration-minutes must be a positive integer")
        sys.exit(2)

    # Parse/validate --cases against the matrix's actual case names (built from
    # the resolved args). "all" (default) selects every case; otherwise every
    # named case must exist, else error listing the valid names.
    all_case_names = [c["name"] for c in build_case_list(args)]
    raw_cases = (args.cases or "all").strip()
    if raw_cases.lower() == "all":
        args.selected_cases = None  # None == run every (post-skip) case
    else:
        requested = [c.strip() for c in raw_cases.split(",") if c.strip()]
        unknown = [c for c in requested if c not in all_case_names]
        if unknown:
            log(f"❌ --cases contains unknown case name(s): {unknown}. Valid case names "
                f"(after --skip-forged-sni filtering) are: {all_case_names}")
            sys.exit(2)
        args.selected_cases = requested
    return args


def preflight_checks(sess, args):
    """Validate every launch precondition that does NOT require provisioning,
    BEFORE a single billable resource is created. These are the cheap failures
    that previously only surfaced ~8 minutes into a run (after VPC/NAT/firewall
    were already up and billing) — e.g. IMAGE_ARN not set because scripts/config.sh
    wasn't sourced. Catching them here fails in ~2 seconds instead. Returns a list
    of human-readable problem strings; empty list means all checks passed."""
    problems = []

    # 1. Image ARN must be set (sourced from scripts/config.sh). This is the exact
    #    gap that cost a full provision cycle: resolve_image_arn() only checks this
    #    inside launch_microvm(), after everything is provisioned.
    if not IMAGE_ARN_BASE:
        problems.append(
            "IMAGE_ARN / MICROVM_IMAGE_ARN is not set — run `source scripts/config.sh` "
            "before launching this test (or export IMAGE_ARN).")
    # 2. Exec role ARN must be set — also only checked inside launch_microvm() today.
    if not EXEC_ROLE_ARN:
        problems.append(
            "EXEC_ROLE_ARN / MICROVM_EXEC_ROLE_ARN is not set — run "
            "`source scripts/config.sh` before launching this test.")

    # 3. If the image ARN is set, confirm the specific per-arch/mem image exists in
    #    this account/region, so a typo or an un-built image tier fails now, not at
    #    VM-launch time after provisioning. get-microvm-image is the lightweight check.
    if IMAGE_ARN_BASE:
        image_arn = f"{IMAGE_ARN_BASE}-{args.arch}-{args.memory}"
        try:
            lam_vm = sess.client("lambda")
            # The MicroVM image API lives under the lambda client in this SDK build;
            # fall back gracefully if the op isn't present rather than hard-failing
            # preflight on an SDK-shape difference.
            if hasattr(lam_vm, "get_microvm_image"):
                lam_vm.get_microvm_image(Identifier=image_arn)
            else:
                log(f"  (preflight note: get_microvm_image not available in this SDK "
                    f"build — skipping image existence check for {image_arn})")
        except ClientError as e:
            if "ResourceNotFound" in str(e) or "NotFound" in str(e):
                problems.append(
                    f"MicroVM image not found for arch={args.arch} memory={args.memory}: "
                    f"{image_arn} — build the image tier or check --arch/--memory.")
            else:
                # Don't block the run on an unexpected API/permission error here;
                # note it and let the real launch surface it with full context.
                log(f"  (preflight note: could not verify image {image_arn}: {e})")

    return problems


def main():
    args = parse_args()
    if args.run_id:
        validate_run_id(args.run_id)

    if not acquire_lock():
        return 1
    try:
        sess = _session()
        sts = sess.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        region = sess.region_name or AWS_REGION

        run_id, resuming = resolve_run_id(args.run_id)
        log(f"Run id: {run_id}  (resuming={resuming})")
        log(f"Caller identity: account={account_id} arn={identity['Arn']}")
        log(f"Region: {region}  Profile: {AWS_PROFILE}")

        # Pre-flight: validate launch preconditions BEFORE provisioning anything
        # billable. Fails in ~2s instead of ~8 minutes if a precondition (e.g.
        # IMAGE_ARN not sourced from scripts/config.sh) is missing.
        problems = preflight_checks(sess, args)
        if problems:
            log("❌ Pre-flight checks failed — NOT provisioning any resources:")
            for p in problems:
                log(f"   • {p}")
            return 2
        log("Pre-flight checks passed — all launch preconditions satisfied.")

        print_cost_estimate(args.test_duration_minutes)
        print(f"About to provision: VPC, 3 subnets, IGW, EIP, NAT Gateway, Network Firewall "
              f"rule group + policy + firewall, route tables, security group, operator IAM "
              f"role, VPC egress network connector, and one MicroVM.")
        print(f"Allowlist: {args.allowlist}")
        print(f"Deny-test domain: {args.deny_test_domain}")
        if not confirm("This will create billable AWS infrastructure.", args.yes):
            log("Aborted by user — nothing created.")
            return 0

        if not resuming:
            _set_entry_fields(run_id, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               region=region, account_id=account_id, status="in_progress", test_results=None)

        ec2 = sess.client("ec2")
        nf = sess.client("network-firewall")
        lam_core = sess.client("lambda-core")
        lam_vm = sess.client("lambda-microvms")
        logs_client = sess.client("logs")
        cw = sess.client("cloudwatch")
        _session_cache["iam"] = sess.client("iam")

        try:
            infra = provision(ec2, nf, lam_core, logs_client, run_id, args)
            vm_id, endpoint = launch_microvm(lam_vm, run_id, infra["connector_arn"], args)

            token = get_auth_token(lam_vm, vm_id)
            log("Waiting for the VM's app server to respond on /health (control-plane RUNNING "
                "only means the control plane launched it, not that the app is serving)...")
            if not wait_for_health(endpoint, token, timeout=180):
                raise RuntimeError(f"MicroVM {vm_id} never responded 200 on /health within timeout")
            log("VM is healthy — running the connectivity smoke test before trusting the deny case.")

            # Firewall name + AZ, needed for the deny case's CloudWatch metric
            # dimensions (FirewallName/AvailabilityZone/Engine=Stateful).
            fw_status = nf.describe_firewall(FirewallArn=infra["firewall_arn"])
            firewall_name = fw_status["Firewall"]["FirewallName"]
            sync_states = fw_status["FirewallStatus"].get("SyncStates", {})
            availability_zone = next(iter(sync_states), None)
            if not availability_zone:
                raise RuntimeError(f"could not determine the firewall's AvailabilityZone from describe_firewall "
                                    f"for metric queries ({infra['firewall_arn']})")

            smoke_ok, smoke_detail = run_smoke_test(endpoint, token, args)
            if not smoke_ok:
                # Per investigate-l7-egress-block/report.md recommendation #1: a
                # deny-case timeout in this situation is indistinguishable from
                # the data path being broken, so the whole run is INCONCLUSIVE —
                # NOT a deny PASS — and the matrix is never even attempted. The
                # smoke test remains the gate before the matrix, exactly as before.
                results = {
                    "smoke": {"verdict": "INCONCLUSIVE", "evidence": smoke_detail,
                              "reason": "egress path not forwarding — smoke test failed"},
                    "cases": [],
                }
                _set_entry_fields(run_id, status="completed", test_results=results)
                print("")
                print("=" * 60)
                print(f"  RESULT TABLE — run id: {run_id}")
                print("=" * 60)
                print(f"  smoke test: INCONCLUSIVE — egress path not forwarding ({smoke_detail})")
                print("  matrix: NOT RUN (smoke-test gate did not pass)")
                print(f"  firewall logs: {infra['log_group_name']}")
                print("=" * 60)
                print(f"Run NetworkEgressTeardown.py --run-id {run_id} --yes when you are done.")
                return 2

            log("Smoke test passed — egress path is forwarding traffic. Running the case matrix.")

            # Build the matrix, apply --cases selection (None == all post-skip cases).
            all_cases = build_case_list(args)
            if args.selected_cases is not None:
                selected = [c for c in all_cases if c["name"] in args.selected_cases]
            else:
                selected = all_cases

            ctx = {
                "endpoint": endpoint, "token": token, "cw": cw,
                "logs_client": logs_client, "firewall_name": firewall_name,
                "availability_zone": availability_zone,
                "log_group_name": infra["log_group_name"], "args": args,
            }
            case_records = run_case_matrix(ctx, selected)
            results = {"smoke": {"verdict": "PASS", "evidence": smoke_detail}, "cases": case_records}
            _set_entry_fields(run_id, status="completed", test_results=results)

            # Aggregate: a case "meets expectation" per _expectation_met(); the run
            # PASSes only if EVERY case met its expectation.
            selected_by_name = {c["name"]: c for c in selected}
            any_fail = False
            any_inconclusive = False
            for rec in case_records:
                case = selected_by_name[rec["name"]]
                if not _expectation_met(case, rec["verdict"]):
                    if rec["verdict"] == "INCONCLUSIVE":
                        any_inconclusive = True
                    else:
                        any_fail = True

            print()
            print("=" * 72)
            print(f"  RESULT TABLE — run id: {run_id}")
            print("=" * 72)
            print("  smoke test: PASS — egress path forwarding")
            print(f"  {'CASE':<28} {'EXPECTED':<28} {'VERDICT':<16} MET?")
            print("  " + "-" * 68)
            for rec in case_records:
                case = selected_by_name[rec["name"]]
                met = "yes" if _expectation_met(case, rec["verdict"]) else "NO"
                print(f"  {rec['name']:<28} {rec['expected']:<28} {rec['verdict']:<16} {met}")
                print(f"      -> {rec['reason']}")
            print("  " + "-" * 68)
            print(f"  firewall logs: {infra['log_group_name']}")
            print("=" * 72)
            print(f"Run NetworkEgressTeardown.py --run-id {run_id} --yes when you are done.")

            # Exit-code convention (extends the prior two-tier distinction):
            #   1 == any case FAILED its expectation (a real failure)
            #   2 == no FAIL but at least one case is INCONCLUSIVE (can't look
            #        like a clean pass)
            #   0 == every case met its expectation (KNOWN_LIMITATION cases count
            #        as met-expectation when the documented behavior held)
            if any_fail:
                return 1
            if any_inconclusive:
                return 2
            return 0
        except Exception as e:
            log(f"❌ run failed: {e}")
            _set_entry_fields(run_id, status="failed")
            log(f"State file left at '{STATE_FILE}' with status=failed — run "
                f"NetworkEgressTeardown.py --run-id {run_id} to clean up what was created.")
            return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
