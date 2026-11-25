# Network Egress Control (Layer 7)

Guidance for restricting **which domains** MicroVM user code can reach, for
production deployments with compliance requirements. For what the project is and
how it works, see the [README](README.md). For the rest of the security posture, see
[SECURITY.md](SECURITY.md).

This page has three parts: what the project does by default, two ways to enforce
domain filtering (Option A and Option B), and what the live test of Option A proved.
Option A (AWS Network Firewall) has been built and run end-to-end with the harness in
`tests/NetworkEgress/`. With an allowlist of `pypi.org` and `.amazonaws.com`, a
request to `pypi.org` returned HTTP 200 and a request to the non-allowlisted
`example.com` was dropped at the TLS SNI layer, and the firewall's ALERT log recorded
the drop. Option B (an in-VM proxy) is a design sketch. No code in this repository
implements it, and it has not been tested.

## Default behavior in this project

MicroVMs have **unrestricted internet access by default**. Nothing on this page is
part of a normal `./local_install.sh` run.

- **One egress connector per launch.** Every MicroVM the proxy launches (the
  `/launch` route and the eternal-mode recovery launch) and every spare the pool
  manager mints is started with `egressNetworkConnectors` set to a single connector:
  the AWS-managed `INTERNET_EGRESS` connector of the region
  (`arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:INTERNET_EGRESS`).
  See `EGRESS_CONNECTOR` in `proxy/platform/microvm_manager.py` and
  `egress_connector` in `pool_manager/backend_factory.py` and `aws_backend.py`.
- **Override with `MICROVM_EGRESS_CONNECTOR`.** Set it to a connector ARN in the
  environment of the proxy and the pool manager. The proxy and pool manager read it
  when they start, and `scripts/config.sh` does not set it, so export it in
  the shell that runs `./local_install.sh`. The ingress connectors have matching
  overrides, `MICROVM_INGRESS_CONNECTOR` and `MICROVM_SHELL_INGRESS_CONNECTOR`.
- **The value replaces the default.** The launch passes a list with one entry, so
  a VPC connector replaces `INTERNET_EGRESS` instead of adding to it. All traffic then
  leaves through your VPC and its firewall.
- **A connector is fixed at launch.** A running VM, and a spare already in the warm
  pool, keeps the connector it was launched with. After changing the variable,
  restart the proxy and the pool manager and let the old VMs and spares go.
- **The managed egress path only passes ports 80, 443 and 53**, as confirmed
  empirically (see the comment in `scripts/config.sh`). This is why a MicroVM reaches
  Amazon Aurora through the RDS Data API over HTTPS instead of a direct database
  socket.
- **What an allowlist has to cover.** Notebook cells call AWS service endpoints
  (`.amazonaws.com`), `pip install` needs the PyPI hosts, and a terminal can run
  `git clone`. Add whatever else your users' code calls. The examples below start
  from this set.

## Option A: AWS Network Firewall

Route MicroVM traffic through a VPC with AWS Network Firewall for infrastructure-enforced domain filtering. See [AWS Network Firewall — Stateful domain list rule groups](https://docs.aws.amazon.com/network-firewall/latest/developerguide/stateful-rule-groups-domain-names.html) for the authoritative behavior of the domain rules used below.

### Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐     ┌──────────┐
│  MicroVM    │────▶│  VPC NAT Gateway │────▶│  AWS Network        │────▶│ Internet │
│  (Lambda)   │     │  (private subnet)│     │  Firewall           │     │          │
└─────────────┘     └──────────────────┘     │  (L7 domain rules)  │     └──────────┘
                                             └─────────────────────┘
```

Instead of the default `INTERNET_EGRESS` connector, MicroVMs are launched into a **VPC private subnet** with a NAT Gateway. All outbound traffic passes through AWS Network Firewall, which inspects TLS SNI (Server Name Indication) to filter by domain.

### Setup Steps

#### 1. Create a VPC with Network Firewall

```bash
# Create VPC with public + private + firewall subnets
aws ec2 create-vpc --cidr-block 10.0.0.0/16

# IMPORTANT: pin the subnets to an AZ where the MicroVm compute type is serviceable
# (pass --availability-zone explicitly). If you let AWS auto-pick, the subnets can
# land in an AZ that does not offer MicroVMs, and step 6's CreateNetworkConnector
# then fails with "Availability zone <az-id> is not available for compute type
# MicroVm". We observed usw2-az4 rejected in us-west-2. There is no API that lists
# "AZs that support MicroVm", so choose a known-good AZ (e.g. us-west-2a) and keep
# all three subnets in the SAME AZ so the firewall endpoint, NAT GW, and connector
# ENIs line up.

# Private subnet (MicroVM egress connector anchors its ENIs here)
aws ec2 create-subnet --vpc-id vpc-xxx --cidr-block 10.0.1.0/24 --availability-zone us-west-2a

# Firewall subnet (Network Firewall ENIs)
aws ec2 create-subnet --vpc-id vpc-xxx --cidr-block 10.0.2.0/24 --availability-zone us-west-2a

# Public subnet (NAT Gateway → Internet Gateway)
aws ec2 create-subnet --vpc-id vpc-xxx --cidr-block 10.0.3.0/24 --availability-zone us-west-2a
```

#### 2. Create Network Firewall Domain Allowlist

The `RulesSourceList` fields below (`Targets`, `TargetTypes`, `GeneratedRulesType`) are the ones in the [`create-rule-group` API](https://docs.aws.amazon.com/network-firewall/latest/APIReference/API_RulesSourceList.html) / [CLI reference](https://awscli.amazonaws.com/v2/documentation/api/latest/reference/network-firewall/create-rule-group.html).

```bash
# Create a stateful rule group with domain filtering
aws network-firewall create-rule-group \
  --rule-group-name "microvm-egress-allowlist" \
  --type STATEFUL \
  --capacity 100 \
  --rule-group '{
    "RulesSource": {
      "RulesSourceList": {
        "Targets": [
          ".amazonaws.com",
          ".aws.amazon.com",
          "pypi.org",
          "files.pythonhosted.org",
          "github.com",
          "raw.githubusercontent.com",
          "api.openai.com",
          "bedrock-runtime.us-west-2.amazonaws.com"
        ],
        "TargetTypes": ["TLS_SNI", "HTTP_HOST"],
        "GeneratedRulesType": "ALLOWLIST"
      }
    }
  }'
```

This allows MicroVMs to reach:
- **AWS services** (S3, DynamoDB, Athena, Bedrock) — required for data access
- **PyPI** — for `pip install` of user packages
- **GitHub** — for package downloads that reference GitHub
- **Everything else is BLOCKED** — no data exfiltration to unauthorized endpoints

> **Allowlist semantics depend on action order.** The implicit "deny everything not
> on the list" that an `ALLOWLIST` domain rule group adds only applies when the
> firewall policy uses **default (action) rule order** — under *strict* order it does
> not. Keep the policy on default order (as below), and avoid mixing `Alert`/`Reject`
> domain rule groups with the allowlist, or the allowlist's default-drop can fire
> first.

#### 3. Create Firewall Policy

```bash
aws network-firewall create-firewall-policy \
  --firewall-policy-name "microvm-egress-policy" \
  --firewall-policy '{
    "StatelessDefaultActions": ["aws:forward_to_sfe"],
    "StatelessFragmentDefaultActions": ["aws:forward_to_sfe"],
    "StatefulRuleGroupReferences": [
      {
        "ResourceArn": "arn:aws:network-firewall:us-west-2:ACCOUNT:stateful-rulegroup/microvm-egress-allowlist"
      }
    ]
  }'
```

#### 4. Deploy the Firewall

```bash
aws network-firewall create-firewall \
  --firewall-name "microvm-egress-firewall" \
  --vpc-id vpc-xxx \
  --subnet-mappings SubnetId=subnet-firewall \
  --firewall-policy-arn "arn:aws:network-firewall:us-west-2:ACCOUNT:firewall-policy/microvm-egress-policy"
```

#### 5. Route MicroVM Traffic Through Firewall

Update route tables so the private subnet (where MicroVMs run) sends `0.0.0.0/0` traffic to the Network Firewall endpoint, which then forwards allowed traffic to the NAT Gateway → Internet.

```bash
# Private subnet route table → Firewall endpoint
aws ec2 create-route \
  --route-table-id rtb-private \
  --destination-cidr-block 0.0.0.0/0 \
  --vpc-endpoint-id vpce-firewall-endpoint
```

#### 6. Attach a VPC egress connector to the MicroVM

MicroVM network access is set by **egress network connectors** attached **at
`run-microvm` time** (they can't be changed while a MicroVM is running). By default a
MicroVM has public internet egress; to force all egress through your VPC (and thus
the firewall), create a **VPC egress connector** and attach it on launch. See
[Lambda MicroVMs — Networking](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html)
for the connector model, the operator-role prerequisites, and connector states.

First create the connector once. It provisions ENIs for the egress path and must
reach `ACTIVE` before use (can take up to ~10 minutes). Two things that bit us in
testing and aren't obvious from the quickstart:

- **The operator role needs `ec2:CreateTags`, not just `ec2:CreateNetworkInterface`.**
  AWS's documented minimum policy grants `ec2:CreateNetworkInterface` **and**
  `ec2:CreateTags` (the latter scoped to `arn:aws:ec2:*:*:network-interface/*` with a
  `Condition` of `ec2:ManagedResourceOperator = network-connectors.lambda.amazonaws.com`).
  Omitting `CreateTags` is a latent gap — grant both.
- **The connector's ENIs are NOT visible to you.** They are Hyperplane-style,
  cross-account service-managed interfaces: `describe-network-interfaces` on your
  subnet/security-group returns nothing for them, and a direct lookup of the ENI id
  returns `NotFound` — even while the connector is `ACTIVE` and actively forwarding
  traffic. **Do not** try to verify data-plane health by looking for an ENI in your
  VPC; it will always appear "empty" whether the connector is healthy or broken. The
  reliable readiness signal is the connector's own `State == ACTIVE`; the reliable
  *forwarding* signal is an actual egress request succeeding (see verification below).

```bash
aws lambda-core create-network-connector \
  --name microvm-vpc-egress \
  --configuration '{
    "VpcEgressConfiguration": {
      "SubnetIds": ["subnet-private"],
      "SecurityGroupIds": ["sg-xxx"],
      "NetworkProtocol": "IPv4",
      "AssociatedComputeResourceTypes": ["MicroVm"]
    }
  }' \
  --operator-role arn:aws:iam::ACCOUNT:role/NetworkConnectorOperatorRole
```

Then pass its ARN on every launch. In this project, export it as
`MICROVM_EGRESS_CONNECTOR` before starting the proxy and the pool manager (see
"Default behavior in this project" above); both launch paths put it in the
`egressNetworkConnectors` list of the `run-microvm` call, so no code change is
needed. To launch by hand with the AWS CLI, use `--egress-network-connectors`:

```bash
aws lambda-microvms run-microvm \
  --image-identifier arn:aws:lambda:us-west-2:ACCOUNT:microvm-image:notebook-microvm-arm64-2048 \
  --egress-network-connectors arn:aws:lambda:us-west-2:ACCOUNT:network-connector:microvm-vpc-egress
```

For an x86_64 VM, use the x86 image of the same tier
(`...:microvm-image:notebook-microvm-x86_64-2048`); the egress connector and
firewall rules are the same for both arches.

With only the VPC egress connector attached (no `INTERNET_EGRESS`), all outbound
traffic leaves through your private subnet → NAT Gateway → firewall. VPC egress is
also subject to your security-group and NACL rules, giving you an L3/L4 layer on top
of the firewall's L7 domain rules.

### Policy Examples

**Minimal (data access only):**
```
ALLOW: .amazonaws.com (S3, DynamoDB, Athena, Bedrock)
DENY: all others
```

**Standard (data + packages):**
```
ALLOW: .amazonaws.com, pypi.org, files.pythonhosted.org
DENY: all others
```

**Permissive (data + packages + APIs):**
```
ALLOW: .amazonaws.com, pypi.org, files.pythonhosted.org, api.github.com, *.openai.com
DENY: all others
```

### Monitoring & Audit

Enable **ALERT** (and optionally **FLOW**) logging to CloudWatch Logs or S3. The
ALERT log is not just an audit trail — it is the **authoritative, per-connection
proof** of what the firewall did (see "Verifying the block" below for why this
matters more than the CloudWatch metrics).

```bash
aws network-firewall update-logging-configuration \
  --firewall-arn arn:aws:network-firewall:... \
  --logging-configuration '{
    "LogDestinationConfigs": [
      {"LogType": "ALERT", "LogDestinationType": "CloudWatchLogs",
       "LogDestination": {"logGroup": "/aws/network-firewall/microvm-egress"}},
      {"LogType": "FLOW",  "LogDestinationType": "CloudWatchLogs",
       "LogDestination": {"logGroup": "/aws/network-firewall/microvm-egress"}}
    ]
  }'
```

> **Two logging gotchas we hit:** (1) `UpdateLoggingConfiguration` only lets you
> add/remove **one `LogDestinationConfig` at a time** — to enable both ALERT and FLOW
> you must call it twice (ALERT, then ALERT+FLOW), and to disable them you remove them
> one at a time. (2) You **cannot delete a firewall while a logging configuration is
> attached** (`DeleteFirewall` → `InvalidRequestException: Cannot delete firewall with
> a logging configuration`) — disable logging first, then delete. Factor both into any
> teardown automation.

This gives you an audit trail of every domain a MicroVM tried to reach — useful for compliance and detecting unauthorized access patterns.

### Verifying the block (ALERT log vs. metrics)

If you want to *confirm* a domain is actually being blocked (not just failing for some
other reason), rely on the **ALERT log**, not on CloudWatch metrics. In our testing a
real block produced an ALERT entry like:

```json
{"event": {
  "app_proto": "tls",
  "tls": {"sni": "example.com", "version": "TLS 1.3"},
  "ts_progress": "client_hello_done",
  "alert": {"signature": "not matching any TLS allowlisted FQDNs", "action": "blocked"},
  "verdict": {"action": "drop"},
  "src_ip": "10.0.1.236"   // the MicroVM, via the egress connector
}}
```

That entry is unambiguous proof: the firewall saw the TLS ClientHello, matched the SNI
against the allowlist, and dropped it. **Do not** rely on the Stateful-engine
`TLSReceivedPackets` / `DroppedPackets` CloudWatch metrics as your sole signal — in a
confirmed-block run those metrics read **zero** for the test window even though the
ALERT log clearly showed the drop, which would make a metrics-only check report a
false "traffic never reached the firewall." Treat the ALERT log as primary and the
metrics as a coarse secondary signal. Also note the ALLOWLIST implicit deny is a
silent **DROP** (the client sees a connect/read **timeout**), not a TCP reset — so a
client-side timeout is the *expected* symptom of a working block, but is only
*confirmation* once the ALERT log corroborates it.

### Cost Considerations

(Rates below are US East / US West; they vary by region. Verify against the
[AWS Network Firewall pricing page](https://aws.amazon.com/network-firewall/pricing/).)

| Component | Cost |
|-----------|------|
| Network Firewall endpoint | $0.395/hr per endpoint, per AZ (~$285/mo per AZ) |
| Firewall traffic processing | $0.065/GB processed |
| NAT Gateway | $0.045/hr + $0.045/GB — **waived** when the NAT GW sits in the same region and the same networking path as the firewall (one-to-one with firewall usage, up to the GB the firewall processes) |

For development/demo, use the default public egress (no VPC needed). For production
with compliance requirements, budget roughly **~$285/mo per AZ** fixed (deploy one
endpoint per AZ for HA → ~$570/mo for 2 AZs) plus **$0.065/GB** processed; with the
NAT-gateway service-chain discount the NAT GW itself is typically $0. (TLS inspection
and active-threat-defense managed rules, if enabled, add further hourly/per-GB
charges — not needed for plain domain filtering.)

## Option B: In-VM Transparent Egress Proxy

An alternative to AWS Network Firewall — bake a lightweight policy-driven proxy inside the MicroVM image. Similar to how **Cilium** enforces L7 policies in Kubernetes pods via eBPF, but implemented as an application-level proxy since MicroVMs don't expose the host kernel.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  MicroVM                                                │
│                                                         │
│  ┌──────────┐     ┌──────────────────-─┐     ┌────────┐ │
│  │ User Code│────▶│ Egress Proxy       │────▶│Network │─┼──▶ Internet
│  │ (Python) │     │ (localhost:8888)   │     │        │ │
│  └──────────┘     │ • Domain allowlist │     └────────┘ │
│                   │ • Path rules       │                │
│  HTTP_PROXY=      │ • Rate limiting    │                │
│  localhost:8888   │ • Audit logging    │                │
│                   └────────┬──────-────┘                │
│                            │                            │
│                   ┌────────▼────────┐                   │
│                   │ Policy (from S3)│                   │
│                   └─────────────────┘                   │
└─────────────────────────────────────────────────────────┘
```

### How it works

1. A small proxy binary (~5MB) is baked into the VM application image (the root `Dockerfile`)
2. At VM boot, the proxy starts and fetches policy from S3: `s3://DOC-EXAMPLE-BUCKET/policies/egress-policy.yaml` (replace `DOC-EXAMPLE-BUCKET` with your own account's bucket)
3. `HTTP_PROXY` / `HTTPS_PROXY` env vars route all Python HTTP traffic through it
4. Every outbound request is checked against the policy — allowed requests pass, denied requests return 403

### Policy Format (centrally managed via S3)

```yaml
# s3://DOC-EXAMPLE-BUCKET/policies/egress-policy.yaml  (replace with your own bucket)
version: 1
default: deny

rules:
  - action: allow
    domains: ["*.amazonaws.com", "*.aws.amazon.com"]
    description: "AWS API access (S3, DynamoDB, Athena, Bedrock)"

  - action: allow
    domains: ["pypi.org", "files.pythonhosted.org"]
    description: "Python package installation"

  - action: allow
    domains: ["api.github.com"]
    paths: ["/repos/*"]
    description: "GitHub API (read-only)"

  - action: deny
    domains: ["*"]
    log: true
    description: "Default deny — block all other egress"
```

Update the YAML in S3 → all new MicroVMs pick up the policy on launch. For running VMs, the proxy can poll S3 periodically for hot-reload.

### Implementation

Multiple open-source proxies can serve this role — no custom binary needed:

| Proxy | Size | L7 Depth | Best For |
|-------|------|----------|----------|
| **Squid** | ~20MB | Domain-level | Simple allowlists, proven in production |
| **mitmproxy** | ~50MB | Full L7 (path, headers, body) | Dynamic policies, Python scripting |
| **Tinyproxy** | ~100KB | Domain-level | Minimal footprint |
| **Envoy** | ~30MB | Full L7 + rate limiting | Istio-like policies without K8s |

**Example with Squid (simplest):**

```dockerfile
# Dockerfile addition (the image is Amazon Linux 2023, so packages come from dnf;
# check that your base image's repositories offer squid)
RUN dnf install -y squid && dnf clean all
COPY squid.conf /etc/squid/squid.conf
ENV HTTP_PROXY=http://localhost:3128
ENV HTTPS_PROXY=http://localhost:3128
ENV NO_PROXY=localhost,127.0.0.1,169.254.169.254
```

```conf
# squid.conf — domain allowlist
acl allowed_domains dstdomain .amazonaws.com .aws.amazon.com
acl allowed_domains dstdomain pypi.org files.pythonhosted.org
acl allowed_domains dstdomain api.github.com raw.githubusercontent.com

http_access allow allowed_domains
http_access deny all

http_port 3128
```

**Example with mitmproxy (full L7 with path rules):**

```dockerfile
RUN pip install mitmproxy
COPY egress_policy.py /opt/egress_policy.py
ENV HTTP_PROXY=http://localhost:8888
ENV HTTPS_PROXY=http://localhost:8888
ENV NO_PROXY=localhost,127.0.0.1,169.254.169.254
```

```python
# egress_policy.py — mitmproxy addon with S3-loaded policy
import json, boto3, mitmproxy.http

POLICY = None

def load_policy():
    global POLICY
    s3 = boto3.client('s3')
    obj = s3.get_object(Bucket='DOC-EXAMPLE-BUCKET', Key='policies/egress-policy.json')  # replace with your own bucket
    POLICY = json.loads(obj['Body'].read())

def request(flow: mitmproxy.http.HTTPFlow):
    if not POLICY:
        load_policy()
    domain = flow.request.host
    path = flow.request.path
    
    for rule in POLICY.get('rules', []):
        if any(domain.endswith(d.lstrip('*')) for d in rule['domains']):
            if rule['action'] == 'allow':
                return  # Allow
    
    # Default deny
    flow.response = mitmproxy.http.Response.make(403, b"Blocked by egress policy")
```

**Boot sequence (illustrative).** Today the image starts only `uvicorn app.server:app`
(see the `CMD` in the root `Dockerfile`), and the platform snapshots the running
process after `/ready` returns. A proxy would have to be started where the app
starts so it is part of that snapshot:
```python
# Start proxy before accepting requests
import subprocess
subprocess.Popen(["squid", "-N"])  # or: ["mitmdump", "-s", "/opt/egress_policy.py", "-p", "8888"]
```

### Security Hardening

User code is not forced to use the proxy. Environment variables set in the image
are only defaults, and code can unset them or open sockets directly. To reduce
bypass:
- **iptables rules**: Force all port 80/443 traffic through the proxy (requires `CAP_NET_ADMIN`)
- **Binary integrity**: Verify proxy binary hash at boot

The image has no `USER` directive and the terminal opens as `root`, so user code
could remove iptables rules unless the capability is dropped for it. Treat Option B
as a policy layer that stops accidental or casual access, not as a boundary against
a hostile notebook. Use Option A when the boundary has to hold.

## Comparison: Network Firewall vs In-VM Proxy

| Feature | AWS Network Firewall | In-VM Transparent Proxy |
|---------|---------------------|------------------------|
| **Monthly cost** | ~$300 fixed + per-GB | $0 (runs inside VM) |
| **Domain filtering** | Yes (TLS SNI inspection) | Yes (CONNECT tunnel) |
| **Path-level rules** | No | Yes (`/api/v1/*` patterns) |
| **Header inspection** | No | Yes (inspect/inject headers) |
| **Rate limiting** | No | Yes (per-domain limits) |
| **Request body inspection** | No | Yes (block large uploads) |
| **VPC required** | Yes (subnets + NAT + routing) | No (works with default INTERNET_EGRESS) |
| **Setup complexity** | High | Low (binary + env var) |
| **Enforcement level** | Network (cannot be bypassed by unsetting a variable) | Application (env-var based) |
| **Bypass risk** | Low (infra-enforced; matching keys on the SNI value the client sends, not the destination address, so a client that forges the SNI is a documented limit) | Real (user code runs as root in the VM; iptables only helps if the capability is dropped for user code) |
| **Central policy** | AWS Console / API | S3 YAML file |
| **Audit logging** | CloudWatch (async) | Inline stdout (real-time) |
| **Hot policy reload** | Immediate (rule update) | Poll-based (60s) |

**Recommendation:**
- **Compliance/regulated workloads** → AWS Network Firewall (enforced outside the VM)
- **Cost-sensitive / flexible policies** → In-VM Proxy (zero infra cost, path-level rules)
- **Defense in depth** → Both (Network Firewall as hard boundary + proxy for fine-grained L7 rules)

---

## Verified behavior & operational gotchas (from the live test)

Option A was exercised end-to-end by the standalone harness in `tests/NetworkEgress/`
(provision VPC + NAT + firewall + VPC egress connector, launch a MicroVM wired to the
connector, then probe an allowed and a denied domain). What the run proved and the
non-obvious pitfalls it surfaced:

**How to run the harness.** It is a one-off script marked `# test-kind: gated`, so
the offline test runner does not run it. It creates billable infrastructure (a
firewall endpoint at about $0.395/hr, plus a NAT gateway and a MicroVM while it runs)
and asks for `--yes` before it creates anything. Run
`python3 tests/NetworkEgress/NetworkEgressTest.py --yes` after sourcing
`scripts/config.sh`, then remove everything with
`python3 tests/NetworkEgress/NetworkEgressTeardown.py --run-id <id> --yes`. The
script needs `IMAGE_ARN` (or `MICROVM_IMAGE_ARN`) and `EXEC_ROLE_ARN` (or
`MICROVM_EXEC_ROLE_ARN`). It launches the MicroVM directly with boto3 and does not go
through the proxy, so it validates the firewall design and does not exercise the
proxy's use of `MICROVM_EGRESS_CONNECTOR`. After a connectivity check, it runs a
10-case matrix from inside the VM: apex and wildcard-suffix allow, several deny
domains, a look-alike suffix deny, an allow-then-redirect trap, plain HTTP on
port 80, a forged-SNI case recorded as a known limitation, and an NXDOMAIN case that
is not counted as a block.

**Confirmed working.** With an ALLOWLIST rule group (`pypi.org`, `.amazonaws.com`,
`files.pythonhosted.org`) on **default** action order:
- Allowed domain (`pypi.org`): HTTP 200 through the egress path.
- Denied domain (`example.com`): dropped by the firewall at the TLS SNI layer, proven
  by the ALERT log entry shown in "Verifying the block" above.
- The same connector forwarded the allowed request and had the denied one dropped, so
  the deny is a genuine firewall rule decision, not a broken path.

**Operational gotchas (each one cost a failed run; all now accounted for above):**

1. **AZ must support the MicroVm compute type.** The connector subnets must be in an
   AZ where MicroVMs are serviceable or `CreateNetworkConnector` fails
   (`Availability zone <az-id> is not available for compute type MicroVm`;
   `usw2-az4` was rejected in us-west-2). Pin subnets to a known-good AZ. No API lists
   the supported AZs.
2. **Operator role needs `ec2:CreateTags`** (condition `ec2:ManagedResourceOperator =
   network-connectors.lambda.amazonaws.com`), in addition to
   `ec2:CreateNetworkInterface`.
3. **Connector ENIs are invisible** (Hyperplane-managed, cross-account): never visible
   via `describe-network-interfaces`, even when healthy and forwarding. Don't gate
   readiness on seeing an ENI — use connector `State == ACTIVE` plus a real egress
   probe.
4. **Confirm blocks via the ALERT log, not metrics.** Stateful `TLSReceivedPackets` /
   `DroppedPackets` can read zero for a window in which a real drop occurred; the ALERT
   log is the authoritative per-flow record.
5. **Logging lifecycle:** add/remove `LogDestinationConfig`s one at a time, and disable
   logging before deleting the firewall.
6. **ALLOWLIST deny = silent DROP** → the client sees a **timeout**, not a reset. A
   timeout alone is not proof of a block; corroborate with the ALERT log.

**Validate an allowed domain carefully.** Use an allowed test domain that returns a
direct 2xx and does **not** HTTP-redirect to a non-allowlisted host. (`s3.amazonaws.com`
307-redirects to `aws.amazon.com`, which an allowlist of `.amazonaws.com` does **not**
cover, so a "working" allow case can still fail on the redirect. `pypi.org` is a safe
choice.)

## References

Official AWS sources used to design and validate this guidance:

**Lambda MicroVM networking**
- [Lambda MicroVMs — Networking](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html) — egress connector model (default public egress vs. customer-managed VPC egress), operator IAM role, connector states (`PENDING`→`ACTIVE`, ~10-min ENI provisioning), and the `run-microvm --egress-network-connectors` attach mechanism.
- [Lambda MicroVMs — Running and using MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html)
- [`RunMicrovm` API reference](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_RunMicrovm.html) — confirms the `egressNetworkConnectors` input.
- [`create-network-connector` CLI reference](https://docs.aws.amazon.com/cli/latest/reference/lambda-core/create-network-connector.html) and [`NetworkConnectorVpcEgressConfiguration`](https://docs.aws.amazon.com/lambda/latest/lambda-core/API_NetworkConnectorVpcEgressConfiguration.html) — the `VpcEgressConfiguration` shape (`SubnetIds`, `SecurityGroupIds`, `NetworkProtocol` ∈ `IPv4`/`DualStack`, `AssociatedComputeResourceTypes` = `MicroVm`).

**AWS Network Firewall (Option A)**
- [Stateful domain list rule groups](https://docs.aws.amazon.com/network-firewall/latest/developerguide/stateful-rule-groups-domain-names.html) — allowlist/denylist behavior, HTTPS-via-SNI and HTTP-via-Host matching, the default-action-order caveat, and the `HOME_NET` requirement for cross-VPC traffic.
- [`RulesSourceList` API reference](https://docs.aws.amazon.com/network-firewall/latest/APIReference/API_RulesSourceList.html) and [`create-rule-group`](https://awscli.amazonaws.com/v2/documentation/api/latest/reference/network-firewall/create-rule-group.html) — the `Targets` / `TargetTypes` (`TLS_SNI`, `HTTP_HOST`) / `GeneratedRulesType` fields.
- [AWS Network Firewall pricing](https://aws.amazon.com/network-firewall/pricing/) — $0.395/endpoint/hr, $0.065/GB processed, and the NAT-gateway service-chain discount.

**In-VM proxy (Option B)**
- [Squid access controls (`acl dstdomain` / `http_access`)](https://wiki.squid-cache.org/SquidFaq/SquidAcl) — the example `squid.conf` filters the **CONNECT target host** for HTTPS (no TLS interception needed). Full HTTPS interception (to see paths/headers inside TLS) would instead require [SslBump Peek-and-Splice](https://wiki.squid-cache.org/Features/SslPeekAndSplice) and a trusted CA in the image.
- [mitmproxy — addon overview](https://docs.mitmproxy.org/stable/addons/overview/) and [event hooks](https://docs.mitmproxy.org/stable/api/events.html) — the `request` hook and `http.Response.make` used in the policy addon.
