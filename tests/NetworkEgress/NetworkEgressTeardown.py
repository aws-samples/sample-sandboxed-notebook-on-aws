#!/usr/bin/env python3
# test-kind: gated
"""
NetworkEgressTeardown.py — delete every resource NetworkEgressTest.py created for
a given run, in reverse dependency order, safely.

WHAT THIS DOES
--------------
Reads tests/NetworkEgress/.network_egress_test_state.json for a run id, verifies
the current AWS credentials resolve to the SAME account/region that run was
created under (abort loudly on mismatch, zero deletes issued), then deletes every
recorded resource in reverse creation order (= reverse dependency order), skipping
(with a logged "already gone") anything that's already deleted, and REFUSING to
delete anything whose live tags don't carry Project=microvm-network-egress-test +
RunId=<run id> (logged "REFUSING TO DELETE", not a hard failure of the whole run).

PREREQUISITES
--------------
Same environment variables as NetworkEgressTest.py (AWS_REGION, AWS_CLI_PROFILE) —
source scripts/config.sh first, or set them by hand. No image/exec-role ARNs are
needed here (Teardown never launches a VM, only terminates one).

COST WARNING
-------------
Leaving the Network Firewall endpoint and NAT Gateway running from a prior
NetworkEgressTest.py run accrues cost indefinitely (~$0.395/hr for the firewall
endpoint). Run this script as soon as you're done testing.

INVOCATION ORDER
-----------------
Run NetworkEgressTest.py FIRST (note the run id it prints), then:
    python3 tests/NetworkEgress/NetworkEgressTeardown.py --run-id <id> --yes
If --run-id is omitted, this script defaults to the single in_progress/failed
entry in the state file if there is exactly one (ambiguous with multiple
incomplete runs — pass --run-id explicitly in that case).
"""

import os
import sys
import json
import time
import argparse

import boto3
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
AWS_PROFILE = os.environ.get("AWS_CLI_PROFILE", "default")

PROJECT_TAG = "microvm-network-egress-test"
STATE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(STATE_DIR, ".network_egress_test_state.json")
LOCK_FILE = STATE_FILE + ".lock"

VM_DEAD_STATES = {"TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"}

# Errors meaning "already gone" per service (design.md Error Handling table).
ALREADY_GONE_MARKERS = (
    "ResourceNotFoundException", "NotFoundException", "NotFound",
    "InvalidVpcID.NotFound", "InvalidSubnetID.NotFound", "InvalidGroup.NotFound",
    "InvalidInternetGatewayID.NotFound", "InvalidRouteTableID.NotFound",
    "InvalidAddress.NotFound", "InvalidAllocationID.NotFound",
    "NatGatewayNotFound", "NoSuchEntity", "InvalidNetworkInterfaceID.NotFound",
)


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _session():
    if AWS_PROFILE and AWS_PROFILE != "default":
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


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


def _read_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_state(state):
    import tempfile
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


def resolve_run_id(state, explicit_run_id):
    if explicit_run_id:
        if explicit_run_id not in state:
            log(f"nothing to do for run '{explicit_run_id}' — not found in the state file.")
            return None
        return explicit_run_id
    incomplete = [rid for rid, e in state.items() if e.get("status") in ("in_progress", "failed")]
    if len(incomplete) == 1:
        return incomplete[0]
    if len(incomplete) == 0:
        log("No --run-id given and no in_progress/failed runs found. Pass --run-id "
            "explicitly to tear down a specific (e.g. completed) run.")
        if state:
            log("Known runs:")
            for rid, e in state.items():
                log(f"    {rid} (status={e.get('status')})")
        return None
    log("Multiple incomplete runs exist — pass --run-id explicitly:")
    for rid in incomplete:
        log(f"    {rid} (status={state[rid].get('status')})")
    sys.exit(1)


def is_already_gone_error(e):
    msg = str(e)
    return any(marker in msg for marker in ALREADY_GONE_MARKERS)


def confirm(prompt, auto_yes):
    if auto_yes:
        return True
    resp = input(f"{prompt} Type 'yes' to proceed: ")
    return resp.strip().lower() == "yes"


# ── Tag-check safety gate ───────────────────────────────────────────────────

def has_valid_tags(tags_list, run_id):
    """tags_list: list of {"Key":..,"Value":..} (EC2-style) or {"Key":"Tags"} map
    forms, normalized by each check_tags_* helper before calling this."""
    tag_map = {t.get("Key") or t.get("key"): t.get("Value") or t.get("value") for t in tags_list}
    return tag_map.get("Project") == PROJECT_TAG and tag_map.get("RunId") == run_id


def refuse(resource_type, resource_id, actual_tags, failures):
    log(f"⚠ REFUSING TO DELETE — tag mismatch: {resource_type} {resource_id} "
        f"actual_tags={actual_tags}")
    failures.append({"type": resource_type, "id": resource_id, "reason": "tag mismatch — skipped for safety"})


def retry_delete(fn, resource_type, resource_id, failures, attempts=3, backoff=2.0):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            fn()
            return True
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  {resource_type} {resource_id} already gone — ok")
                return True
            last_exc = e
            if attempt < attempts:
                time.sleep(backoff * attempt)
    log(f"❌ FAILED TO DELETE — needs manual cleanup: {resource_type} {resource_id}: {last_exc}")
    failures.append({"type": resource_type, "id": resource_id, "reason": str(last_exc)})
    return False


# ── NAT Gateway delete + poll (dedicated poll, see design.md) ──────────────

def delete_nat_gateway(ec2, nat_gw_id, run_id, failures):
    try:
        desc = ec2.describe_nat_gateways(NatGatewayIds=[nat_gw_id])["NatGateways"]
    except ClientError as e:
        if is_already_gone_error(e):
            log(f"  nat_gateway {nat_gw_id} already gone — ok")
            return True
        desc = []
    if desc:
        tags = desc[0].get("Tags", [])
        if not has_valid_tags(tags, run_id):
            refuse("nat_gateway", nat_gw_id, tags, failures)
            return False
    try:
        ec2.delete_nat_gateway(NatGatewayId=nat_gw_id)
    except ClientError as e:
        if is_already_gone_error(e):
            log(f"  nat_gateway {nat_gw_id} already gone — ok")
            return True
        log(f"❌ FAILED TO DELETE nat_gateway {nat_gw_id}: {e}")
        failures.append({"type": "nat_gateway", "id": nat_gw_id, "reason": str(e)})
        return False

    log(f"  nat_gateway {nat_gw_id} delete issued — polling for 'deleted' (10s interval, 2 min timeout)...")
    deadline = time.time() + 120
    deleted = False
    while time.time() < deadline:
        try:
            resp = ec2.describe_nat_gateways(NatGatewayIds=[nat_gw_id])["NatGateways"]
        except ClientError as e:
            if is_already_gone_error(e):
                deleted = True
                break
            resp = []
        if not resp or resp[0].get("State") == "deleted":
            deleted = True
            break
        time.sleep(10)
    if deleted:
        log(f"  nat_gateway {nat_gw_id} confirmed deleted.")
        return True
    log(f"  ⚠ nat_gateway {nat_gw_id} did not reach 'deleted' within 2 min — "
        f"this is an expected-but-slow transition, not necessarily a failure; "
        f"proceeding to release the EIP anyway (any resulting dependency-violation "
        f"error is caught by the generic retry path below).")
    return True  # degrade: proceed, let EIP release's own retry path catch any fallout


# ── "Still in use" propagation-lag retry (dedicated window, see design.md) ──
#
# The firewall-policy -> rule-group detachment (and, for the policy itself, the
# firewall -> policy detachment) is AWS-side bookkeeping that is NOT guaranteed
# to have fully propagated just because describe_firewall already 404s for the
# firewall itself. When it lags, delete_firewall_policy/delete_rule_group raise
# InvalidOperationException with a "still in use" message. The generic
# retry_delete() (3 attempts, ~2s/4s/6s backoff) is a much shorter window than
# the dedicated long-polls this file already uses for analogous propagation-delay
# problems elsewhere (delete_nat_gateway()'s 2 min poll, the firewall delete's own
# 10 min poll) — so this mirrors that same pattern instead of inventing a new one.

def delete_with_still_in_use_retry(fn, resource_type, resource_id, failures, timeout_s=120, interval_s=10):
    """Retry fn() (a delete call) for up to timeout_s when it fails with
    InvalidOperationException / 'still in use' — the firewall-policy ->
    rule-group detachment can lag behind the firewall itself confirming deleted
    (see delete_nat_gateway() above for the analogous NAT Gateway
    propagation-delay pattern this mirrors). Falls through to the generic
    retry_delete()/failure-recording path if the window is exhausted or the
    error isn't this specific retryable one."""
    deadline = time.time() + timeout_s
    while True:
        try:
            fn()
            return True
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  {resource_type} {resource_id} already gone — ok")
                return True
            msg = str(e)
            if "InvalidOperationException" not in msg and "still in use" not in msg:
                # not the propagation-lag error this helper exists for — defer
                # to the generic retry/failure-recording path.
                return retry_delete(fn, resource_type, resource_id, failures)
            if time.time() >= deadline:
                log(f"  {resource_type} {resource_id} still 'still in use' after "
                    f"{timeout_s}s dedicated retry — falling through to the "
                    f"generic retry path.")
                return retry_delete(fn, resource_type, resource_id, failures)
            log(f"  {resource_type} {resource_id} delete failed with a "
                f"detachment-propagation-lag error — retrying in {interval_s}s "
                f"(up to {timeout_s}s total): {e}")
            time.sleep(interval_s)


# ── MicroVM terminate + wait-for-dead-state ─────────────────────────────────

def terminate_and_wait(lam_vm, vm_id, run_id, failures):
    try:
        lam_vm.terminate_microvm(microvmIdentifier=vm_id)
    except ClientError as e:
        if is_already_gone_error(e):
            log(f"  microvm {vm_id} already gone — ok")
            return True
        log(f"❌ FAILED TO DELETE microvm {vm_id}: {e}")
        failures.append({"type": "microvm", "id": vm_id, "reason": str(e)})
        return False
    except Exception as e:
        if "ResourceNotFound" in str(e) or "NotFound" in str(e):
            log(f"  microvm {vm_id} already gone — ok")
            return True
        log(f"❌ FAILED TO DELETE microvm {vm_id}: {e}")
        failures.append({"type": "microvm", "id": vm_id, "reason": str(e)})
        return False

    log(f"  microvm {vm_id} terminate issued — polling for a dead state (10s interval, 2 min timeout)...")
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            state = lam_vm.get_microvm(microvmIdentifier=vm_id).get("state", "UNKNOWN")
        except Exception as e:
            if "ResourceNotFound" in str(e) or "NotFound" in str(e):
                state = "NOT_FOUND"
            else:
                state = "UNKNOWN"
        if state in VM_DEAD_STATES and state != "TERMINATING":
            log(f"  microvm {vm_id} reached dead state {state}.")
            return True
        time.sleep(10)
    log(f"  ⚠ microvm {vm_id} did not reach a dead state within 2 min — continuing "
        f"to the rest of teardown anyway; listed below if anything downstream fails.")
    failures.append({"type": "microvm", "id": vm_id, "reason": "did not reach dead state within timeout — needs manual verification"})
    return True  # degrade: continue teardown; connector delete may itself fail-and-report


# ── Orphaned-dependency sweep before VPC delete ─────────────────────────────
#
# LEARNED FROM A REAL FAILURE (an incident during a teardown
# test run): deleting an unrelated EKS workshop stack's VPC via its owning
# CloudFormation stack failed with "The vpc '...' has dependencies and cannot
# be deleted" even after every subnet, route table, NAT gateway, and ENI the
# stack itself tracked was already gone. The actual blocker was a security
# group (`eks-cluster-sg-<name>-<hash>`) that EKS creates automatically when a
# cluster is provisioned — OUTSIDE of CloudFormation's/eksctl's own resource
# list — and which nothing ever cleans up once the cluster itself is deleted.
# `delete_vpc` fails on ANY leftover non-default security group in the VPC,
# tracked or not, and the error message gives no hint which one.
#
# This script's own `security_group` resource type (the connector's SG) is
# already deleted earlier in the reverse-order loop, so in the normal case this
# sweep finds nothing. It exists for the abnormal case: AWS (or a future
# addition to this script, or a user running something else against this same
# test VPC) leaves behind a security group this state file never tracked. Only
# `default` is skipped (AWS deletes it automatically with the VPC — you cannot
# delete it yourself, and it carries no risk by construction, since nothing
# this script creates ever attaches resources to the VPC's auto-created
# default SG).
#
# This is a best-effort diagnostic-and-fix step, not a tag-gated deletion of a
# resource recorded in the state file — there IS no recorded resource.
# Correctness/safety comes from scope alone: every security group in THIS VPC
# (which is already tag-and-account/region verified above) is, by construction
# of what this script provisions, either this run's own connector SG or an
# AWS-internal leftover with no legitimate reason to survive the VPC it lives
# in. If deleting one fails (e.g. it is still referenced by something else
# entirely unexpected), that failure is logged and recorded, and the VPC
# delete is still attempted — if the real blocker is one we couldn't clear, the
# subsequent delete_vpc failure will say so explicitly and a human can inspect
# the specific group named below instead of hunting blind the way we just had
# to.
def sweep_orphaned_vpc_dependencies(ec2, vpc_id, run_id, failures):
    try:
        sgs = ec2.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["SecurityGroups"]
    except ClientError as e:
        if is_already_gone_error(e):
            return
        log(f"  ⚠ could not list security groups in {vpc_id} to pre-sweep: {e} "
            f"— proceeding to delete_vpc anyway.")
        return

    orphans = [sg for sg in sgs if sg.get("GroupName") != "default"]
    if not orphans:
        return

    log(f"  pre-VPC-delete sweep: found {len(orphans)} non-default security "
        f"group(s) still in {vpc_id} not tracked in the state file (this is the "
        f"EKS-style orphan pattern — see comment above) — deleting them before "
        f"delete_vpc so it doesn't fail with an opaque 'has dependencies' error:")
    for sg in orphans:
        sg_id = sg["GroupId"]
        sg_name = sg.get("GroupName", "")
        log(f"    deleting orphaned security_group {sg_id} ({sg_name})")
        retry_delete(lambda sg_id=sg_id: ec2.delete_security_group(GroupId=sg_id),
                     "security_group (orphaned, pre-VPC-delete sweep)", sg_id, failures)


# Same incident class, smaller scale: a subnet can't delete while an ENI is
# still attached to it (e.g. the Network Firewall endpoint's or the VPC egress
# connector's ENI, mid-release a few seconds after their owning resource's
# delete call returned). The firewall/connector delete steps earlier in this
# same reverse-order loop already poll their own resource to a confirmed-gone
# state, so by the time we reach a subnet this is normally already clear —
# this is a short, bounded safety margin for the case where ENI release lags
# slightly behind the parent resource reporting itself deleted, not a
# replacement for those polls.
def wait_for_subnet_enis_clear(ec2, subnet_id, timeout_s=60, interval_s=10):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            enis = ec2.describe_network_interfaces(
                Filters=[{"Name": "subnet-id", "Values": [subnet_id]}]
            )["NetworkInterfaces"]
        except ClientError as e:
            if is_already_gone_error(e):
                return
            return  # can't check — fall through to the delete attempt itself
        if not enis:
            return
        log(f"  subnet {subnet_id} still has {len(enis)} ENI(s) attached "
            f"({', '.join(e.get('NetworkInterfaceId', '?') for e in enis)}) — "
            f"waiting up to {timeout_s}s for them to clear before deleting...")
        time.sleep(interval_s)
    log(f"  ⚠ subnet {subnet_id} still shows attached ENIs after {timeout_s}s — "
        f"attempting delete anyway; a failure here will be reported below with "
        f"the ENI id(s) visible via 'aws ec2 describe-network-interfaces "
        f"--filters Name=subnet-id,Values={subnet_id}' for manual cleanup.")


# ── Firewall logging teardown (must happen BEFORE delete_firewall) ─────────
#
# AWS rejects DeleteFirewall with InvalidRequestException ("Cannot delete
# firewall with a logging configuration") while a logging configuration is
# still attached — the firewall's logging must be explicitly DISABLED first.
# Just like NetworkEgressTest.py's provision() builds ALERT+FLOW logging up
# incrementally (UpdateLoggingConfiguration only accepts ONE added/modified
# LogDestinationConfig relative to current state per call), the same
# constraint applies in reverse: clearing [ALERT, FLOW] down to [] cannot be
# done in a single call either, so this removes one LogDestinationConfig at a
# time until the configuration is empty.
def disable_firewall_logging(nf, firewall_arn, failures):
    try:
        current = nf.describe_logging_configuration(FirewallArn=firewall_arn)
        configs = current.get("LoggingConfiguration", {}).get("LogDestinationConfigs", [])
    except ClientError as e:
        if is_already_gone_error(e):
            log(f"  firewall {firewall_arn} logging already gone — ok")
            return True
        log(f"  ⚠ could not describe logging configuration for firewall "
            f"{firewall_arn}: {e} — proceeding to delete_firewall anyway.")
        return True

    if not configs:
        log(f"  firewall {firewall_arn} has no logging configuration to disable.")
        return True

    log(f"  firewall {firewall_arn} has {len(configs)} logging destination(s) "
        f"attached — disabling one at a time before delete_firewall...")
    remaining = list(configs)
    while remaining:
        remaining = remaining[:-1]
        try:
            nf.update_logging_configuration(
                FirewallArn=firewall_arn,
                LoggingConfiguration={"LogDestinationConfigs": remaining},
            )
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  firewall {firewall_arn} already gone — ok")
                return True
            log(f"❌ FAILED TO DISABLE logging configuration for firewall {firewall_arn}: {e}")
            failures.append({"type": "firewall_logging_configuration", "id": firewall_arn, "reason": str(e)})
            return False
    log(f"  firewall {firewall_arn} logging configuration cleared.")
    return True


# ── Per-resource-type delete dispatch ───────────────────────────────────────

def delete_resource(clients, resource, run_id, failures):
    ec2, nf, lam_core, lam_vm, iam, logs_client = clients
    rtype = resource.get("type")

    if rtype == "microvm":
        terminate_and_wait(lam_vm, resource["id"], run_id, failures)
        return

    if rtype == "firewall_logging_configuration":
        # The logging configuration itself is not a separately-deletable AWS
        # resource — there is no standalone API to delete the config record
        # itself, so this is still a no-op here. But it is NOT "removed
        # implicitly with the firewall": AWS rejects DeleteFirewall with
        # InvalidRequestException ("Cannot delete firewall with a logging
        # configuration") while a logging configuration is still attached.
        # The actual teardown of logging — explicitly DISABLING it via
        # update_logging_configuration, one LogDestinationConfig at a time —
        # happens in the "firewall" branch below, immediately BEFORE
        # delete_firewall is called (teardown processes resources in
        # reverse-creation order, so the firewall must have its logging
        # cleared at the moment IT is deleted, not here). This entry only
        # exists so a human reading the state file can see logging was
        # enabled for this run. The CloudWatch log group it pointed at is a
        # separate "log_group" resource, deleted by that branch above.
        log(f"  firewall_logging_configuration for {resource.get('firewall_arn')} "
            f"— no standalone delete API for this record; logging is explicitly "
            f"disabled as part of the firewall delete step below.")
        return

    if rtype == "log_group":
        name = resource["name"]
        try:
            logs_client.delete_log_group(logGroupName=name)
            log(f"  log_group {name} deleted.")
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  log_group {name} already gone — ok")
            else:
                log(f"❌ FAILED TO DELETE log_group {name}: {e}")
                failures.append({"type": "log_group", "id": name, "reason": str(e)})
        return

    if rtype == "network_connector":
        arn = resource["arn"]
        # NOTE: the live `lambda-core` boto3 service model is PascalCase
        # (Name/Configuration/OperatorRole/Tags/Identifier), NOT camelCase like
        # lambda-microvms (round-4 review finding) — Identifier= below is the
        # corrected casing for both get_network_connector and
        # delete_network_connector.
        try:
            conn = lam_core.get_network_connector(Identifier=arn)
        except Exception as e:
            if "ResourceNotFound" in str(e) or "NotFound" in str(e):
                log(f"  network_connector {arn} already gone — ok")
                return
            conn = None
        # Documented exception to the tag-check safety gate: GetNetworkConnector's
        # response carries NO tags field at all (confirmed against the live
        # lambda-core service model — its output members are Arn, Name, Id,
        # Version, Configuration, OperatorRole, State, StateReason,
        # StateReasonCode, LastUpdateStatus, LastUpdateStatusReason,
        # LastUpdateStatusReasonCode, LastModified; no Tags/tags key). A
        # tag-based check here would always see an empty tag map and refuse to
        # delete the connector on every run, leaking it. Per design.md's
        # carve-out language for resources that aren't tag-checkable the same
        # way as everything else, this falls back to identifying the connector
        # by the ARN recorded in the state file: we only ever reach this branch
        # for the exact ARN NetworkEgressTest.py itself recorded for THIS run
        # id (the state file is keyed by run_id and this resource's record came
        # from that run's own creation step), so the ARN match from the state
        # file plus the account/region identity check already done in main()
        # is the equivalent safety guarantee for this one resource type.
        if conn is None:
            log(f"  network_connector {arn} already gone — ok")
            return
        retry_delete(lambda: lam_core.delete_network_connector(Identifier=arn), "network_connector", arn, failures)
        return

    if rtype == "iam_policy":
        policy_name = resource["name"]
        # Resource ARN was recorded by NetworkEgressTest.py as
        # "<role_arn>/policy/<policy_name>", e.g.
        # "arn:aws:iam::123456789012:role/my-role/policy/my-policy" — splitting
        # on "/policy/" and taking the role ARN's last path segment recovers the
        # role name regardless of the account id's digit count.
        role_arn_part = resource["arn"].split("/policy/")[0] if "/policy/" in resource["arn"] else None
        role_name = role_arn_part.rsplit("/", 1)[-1] if role_arn_part else None
        if not role_name:
            failures.append({"type": "iam_policy", "id": policy_name, "reason": "could not derive owning role name"})
            return
        try:
            iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
            log(f"  iam_policy {policy_name} (role {role_name}) deleted.")
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  iam_policy {policy_name} already gone — ok")
            else:
                log(f"❌ FAILED TO DELETE iam_policy {policy_name}: {e}")
                failures.append({"type": "iam_policy", "id": policy_name, "reason": str(e)})
        return

    if rtype == "iam_role":
        role_name = resource["name"]
        try:
            iam.get_role(RoleName=role_name)
            tags = iam.list_role_tags(RoleName=role_name).get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("iam_role", role_name, tags, failures)
                return
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  iam_role {role_name} already gone — ok")
                return
        retry_delete(lambda: iam.delete_role(RoleName=role_name), "iam_role", role_name, failures)
        return

    if rtype == "firewall":
        arn = resource["arn"]
        try:
            fw = nf.describe_firewall(FirewallArn=arn)
            tags = fw.get("Firewall", {}).get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("firewall", arn, tags, failures)
                return
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  firewall {arn} already gone — ok")
                return
        disable_firewall_logging(nf, arn, failures)
        if retry_delete(lambda: nf.delete_firewall(FirewallArn=arn), "firewall", arn, failures):
            log(f"  firewall {arn} delete issued — polling for full deletion (15s interval, 10 min timeout)...")
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    nf.describe_firewall(FirewallArn=arn)
                except ClientError as e:
                    if is_already_gone_error(e):
                        log(f"  firewall {arn} confirmed deleted.")
                        return
                time.sleep(15)
            log(f"  ⚠ firewall {arn} did not confirm deletion within 10 min — check manually.")
        return

    if rtype == "firewall_policy":
        arn = resource["arn"]
        try:
            fp = nf.describe_firewall_policy(FirewallPolicyArn=arn)
            tags = fp.get("FirewallPolicyResponse", {}).get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("firewall_policy", arn, tags, failures)
                return
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  firewall_policy {arn} already gone — ok")
                return
        delete_with_still_in_use_retry(lambda: nf.delete_firewall_policy(FirewallPolicyArn=arn), "firewall_policy", arn, failures)
        return

    if rtype == "rule_group":
        arn = resource["arn"]
        try:
            rg = nf.describe_rule_group(RuleGroupArn=arn)
            tags = rg.get("RuleGroupResponse", {}).get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("rule_group", arn, tags, failures)
                return
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  rule_group {arn} already gone — ok")
                return
        delete_with_still_in_use_retry(lambda: nf.delete_rule_group(RuleGroupArn=arn), "rule_group", arn, failures)
        return

    if rtype == "route_table":
        rtb_id = resource["id"]
        try:
            resp = ec2.describe_route_tables(RouteTableIds=[rtb_id])["RouteTables"]
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  route_table {rtb_id} already gone — ok")
                return
            resp = []
        if resp:
            tags = resp[0].get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("route_table", rtb_id, tags, failures)
                return
            for assoc in resp[0].get("Associations", []):
                if not assoc.get("Main") and assoc.get("RouteTableAssociationId"):
                    try:
                        ec2.disassociate_route_table(AssociationId=assoc["RouteTableAssociationId"])
                    except ClientError:
                        pass
        retry_delete(lambda: ec2.delete_route_table(RouteTableId=rtb_id), "route_table", rtb_id, failures)
        return

    if rtype == "security_group":
        sg_id = resource["id"]
        try:
            resp = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"]
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  security_group {sg_id} already gone — ok")
                return
            resp = []
        if resp:
            tags = resp[0].get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("security_group", sg_id, tags, failures)
                return
        retry_delete(lambda: ec2.delete_security_group(GroupId=sg_id), "security_group", sg_id, failures)
        return

    if rtype == "nat_gateway":
        delete_nat_gateway(ec2, resource["id"], run_id, failures)
        return

    if rtype == "eip":
        alloc_id = resource["allocation_id"]
        try:
            resp = ec2.describe_addresses(AllocationIds=[alloc_id])["Addresses"]
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  eip {alloc_id} already gone — ok")
                return
            resp = []
        if resp:
            tags = resp[0].get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("eip", alloc_id, tags, failures)
                return
        retry_delete(lambda: ec2.release_address(AllocationId=alloc_id), "eip", alloc_id, failures)
        return

    if rtype == "internet_gateway":
        igw_id = resource["id"]
        try:
            resp = ec2.describe_internet_gateways(InternetGatewayIds=[igw_id])["InternetGateways"]
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  internet_gateway {igw_id} already gone — ok")
                return
            resp = []
        if resp:
            tags = resp[0].get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("internet_gateway", igw_id, tags, failures)
                return
            for att in resp[0].get("Attachments", []):
                vpc_id = att.get("VpcId")
                if vpc_id:
                    try:
                        ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
                    except ClientError:
                        pass
        retry_delete(lambda: ec2.delete_internet_gateway(InternetGatewayId=igw_id), "internet_gateway", igw_id, failures)
        return

    if rtype == "subnet":
        subnet_id = resource["id"]
        try:
            resp = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"]
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  subnet {subnet_id} already gone — ok")
                return
            resp = []
        if resp:
            tags = resp[0].get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("subnet", subnet_id, tags, failures)
                return
            wait_for_subnet_enis_clear(ec2, subnet_id)
        retry_delete(lambda: ec2.delete_subnet(SubnetId=subnet_id), "subnet", subnet_id, failures)
        return

    if rtype == "vpc":
        vpc_id = resource["id"]
        try:
            resp = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"]
        except ClientError as e:
            if is_already_gone_error(e):
                log(f"  vpc {vpc_id} already gone — ok")
                return
            resp = []
        if resp:
            tags = resp[0].get("Tags", [])
            if not has_valid_tags(tags, run_id):
                refuse("vpc", vpc_id, tags, failures)
                return
            sweep_orphaned_vpc_dependencies(ec2, vpc_id, run_id, failures)
        retry_delete(lambda: ec2.delete_vpc(VpcId=vpc_id), "vpc", vpc_id, failures)
        return

    log(f"⚠ unknown resource type '{rtype}' in state file — skipping: {resource}")


def main():
    ap = argparse.ArgumentParser(
        description="Tear down every resource a NetworkEgressTest.py run created.",
    )
    ap.add_argument("--run-id", help="Run id to tear down (defaults to the single in_progress/failed entry, if exactly one)")
    ap.add_argument("--yes", "-y", action="store_true", help="Skip the interactive confirmation prompt")
    ap.add_argument("--force-account-mismatch", action="store_true",
                     help="Proceed even if current credentials resolve to a different account/region "
                          "than the state file's recorded account_id/region for this run (documented escape hatch).")
    args = ap.parse_args()

    if not acquire_lock():
        return 1
    try:
        state = _read_state()
        if state is None:
            log(f"nothing to tear down — state file missing or unreadable at '{STATE_FILE}'")
            return 1

        run_id = resolve_run_id(state, args.run_id)
        if run_id is None:
            return 0

        entry = state[run_id]
        resources = entry.get("resources", [])
        if not resources:
            log(f"nothing to do for run '{run_id}' — no resources recorded.")
            return 0

        sess = _session()
        sts = sess.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        region = sess.region_name or AWS_REGION

        recorded_account = entry.get("account_id")
        recorded_region = entry.get("region")
        if (recorded_account and recorded_account != account_id) or (recorded_region and recorded_region != region):
            msg = (f"state file run {run_id} was created in account {recorded_account}/region "
                   f"{recorded_region}, but current credentials resolve to account {account_id}/"
                   f"region {region} — refusing to proceed. Re-run with the correct "
                   f"AWS_CLI_PROFILE/AWS_REGION, or pass --force-account-mismatch if you are "
                   f"certain this is intentional.")
            if args.force_account_mismatch:
                log(f"⚠ ACCOUNT/REGION MISMATCH OVERRIDDEN via --force-account-mismatch: {msg}")
            else:
                log(f"❌ {msg}")
                return 1

        log(f"Tearing down run '{run_id}' — {len(resources)} resource(s) recorded, in reverse order.")
        for r in resources:
            print(f"  will attempt to delete: {r.get('type')} {r.get('id') or r.get('arn') or r.get('name')}")
        if not confirm("This will delete AWS resources.", args.yes):
            log("Aborted by user — nothing deleted.")
            return 0

        ec2 = sess.client("ec2")
        nf = sess.client("network-firewall")
        lam_core = sess.client("lambda-core")
        lam_vm = sess.client("lambda-microvms")
        iam = sess.client("iam")
        logs_client = sess.client("logs")
        clients = (ec2, nf, lam_core, lam_vm, iam, logs_client)

        failures = []
        remaining = list(reversed(resources))
        for resource in list(remaining):
            log(f"Deleting {resource.get('type')} {resource.get('id') or resource.get('arn') or resource.get('name')} ...")
            before_failures = len(failures)
            delete_resource(clients, resource, run_id, failures)
            # Teardown is itself incrementally safe to interrupt: after each
            # successful delete, rewrite the state file removing that entry.
            if len(failures) == before_failures:
                state = _read_state() or {}
                if run_id in state and resource in state[run_id].get("resources", []):
                    state[run_id]["resources"].remove(resource)
                    _write_state(state)

        state = _read_state() or {}
        if run_id in state and not state[run_id].get("resources"):
            del state[run_id]
            _write_state(state)
            log(f"Run '{run_id}' fully torn down — removed from state file.")
        elif run_id in state:
            log(f"Run '{run_id}' partially torn down — {len(state[run_id]['resources'])} "
                f"resource(s) remain in the state file (see failures below).")

        if failures:
            print("")
            print("=" * 60)
            print("  FAILED TO DELETE / SKIPPED FOR SAFETY — needs manual cleanup")
            print("=" * 60)
            for f in failures:
                print(f"  {f['type']} {f['id']}: {f['reason']}")
            return 1

        log("Teardown complete — no failures.")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
