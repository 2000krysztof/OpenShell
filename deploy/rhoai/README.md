# OpenShell and Dashboard installer for OpenShift

This ODH-fork installer installs two independent Helm releases, `openshell` and
`openshell-dashboard`, into the administrator-selected namespace. It reuses an
existing namespace or creates it if missing. Each namespace has one gateway and
its dashboard; reruns upgrade the same releases. The gateway chart supplies
the gateway and the configuration for launching supervisor and sandbox workloads.
The UI chart remains in the [dashboard repository](https://github.com/opendatahub-io/openshell-dashboard/tree/main/deploy/helm/openshell-dashboard).

This is an initial installer, not a qualified RHOAI deployment. Gateway chart
`0.1.3` and dashboard chart `0.1.0`, using their default images, passed the live
smoke checks below on OpenShift `4.21.8`. Matching `0.1` release lines alone do
not prove API compatibility. Choose exact published versions and validate them
together. The script intentionally has no default release versions.

## Live validation

On October 13, 2026, the installer was exercised on an OpenShift `4.21.8` cluster
with RHOAI installed, OVN Kubernetes, `gp3-csi` storage, Agent Sandbox `v1.0.6`, and
an existing Red Hat build of Keycloak `26.4.10`. A dedicated test realm supplied
OIDC; existing customer realms were not changed.

The following checks passed with gateway chart `0.1.3` and dashboard chart `0.1.0`:

- First installation into a new namespace and a second run with the same inputs.
- Installation and rerun in a separately pre-created namespace selected with
  `--namespace`, with both releases reaching revision 2 and the namespace's labels,
  annotations, and OpenShift SCC allocations preserved.
- Gateway readiness, both dashboard containers ready, and persistent storage bound.
- Public HTTPS routing and HTTP 401 for unauthenticated dashboard API requests.
- Browser OIDC redirect/login/callback flow and authenticated workspace and gateway API calls.
- Manual browser confirmation of the workspaces page, platform-admin identity,
  healthy gateway indicator, and active default workspace.
- Dashboard compatibility response reporting the gateway release line as supported.
- Sandbox creation through the dashboard API, workload/supervisor readiness,
  authenticated terminal WebSocket execution with exit code 0, and completed deletion.

An expired, unrelated Kagenti admission webhook initially blocked pod creation.
Removing Kagenti restored both Agent Sandbox and the existing Keycloak instance.
The installer correctly stopped before installing OpenShell while Agent Sandbox
was missing or unhealthy. Diagnose admission webhook failures before changing
OpenShell SCC settings or authentication configuration.

These checks do not establish FIPS qualification, production certificate renewal,
standard-user cross-workspace isolation, or a full UI interaction test. The live
configuration and credentials are kept outside the repository.

## Prerequisites

- `uv`, Helm 3.17.3 or later, and `kubectl` on the administrator's workstation.
- An OpenShift cluster, an explicit kubeconfig context, and permission to manage
  the namespaced/cluster resources required by the charts. Permission to create
  namespaces is needed only when the selected namespace does not exist.
- Agent Sandbox CRDs and a running controller installed by the cluster
  administrator. The installer checks the supported served Sandbox API and waits
  for the controller Deployment. Override `agent_sandbox_namespace` and
  `agent_sandbox_deployment` in the JSON if your installation uses different names.
- A CNI enforcing NetworkPolicy, a working default StorageClass, and cluster SCC
  configuration appropriate for the pinned gateway's sandbox runtime.
- Customer-managed HTTPS OIDC, a confidential browser client with PKCE S256,
  and access tokens whose audience and role claims match the gateway settings.
- Distinct dashboard and gateway DNS names pointing at the OpenShift router.
  The dashboard name must be covered by the router's default certificate.
- Registry access from the workstation for chart pulls and from cluster nodes
  for all images. Private image pull-secret provisioning is outside this first
  installer; use cluster-managed registry credentials if needed.

The installer does not install an IdP, Agent Sandbox, RHOAI, a modular operator,
or console integration. Administrators retain responsibility for these cluster
prerequisites. It does not change your current kubeconfig context.

## Configure and install

Copy the configuration outside the checkout so local credentials cannot be
accidentally committed:

```shell
mkdir -p "$HOME/.config/openshell-rhoai"
chmod 700 "$HOME/.config/openshell-rhoai"
cp deploy/rhoai/config.example.json "$HOME/.config/openshell-rhoai/config.json"
```

Edit the copy. Replace both version placeholders with exact published `0.1.x`
chart versions, and set the namespace, kubeconfig context, DNS names, and OIDC
claims. The two versions need not have the same patch number. The dashboard OCI
reference currently points to its upstream publisher, not an ODH chart registry.
Both chart references are configurable for mirrors.

Save the browser client's secret to `dashboard-client-secret.txt` beside the JSON,
using a trusted editor or your secret manager. Restrict its permissions to `0600`.
Do not put the secret in the JSON, Helm values, or command arguments. File paths
in `oidc.client_secret_file` and optional `oidc.ca_file` are relative to the JSON.
The latter supplies a PEM CA bundle for a private OIDC issuer to both releases.

Register `https://<dashboard host>/oauth2/callback` with your IdP. The browser
client ID is separate from the gateway API audience. Generic OIDC uses
`allowed_groups`; Keycloak can use `provider: "keycloak-oidc"` and `allowed_roles`
such as `openshell-api:openshell-user`. These are alternatives, not assumptions
about customer claim names. Without either UI filter, any authenticated IdP user
can reach the UI, but the gateway still enforces its roles and workspace access.

Validate both charts without connecting to the cluster:

```shell
uv run --script --python 3.12 deploy/rhoai/install.py \
  --config "$HOME/.config/openshell-rhoai/config.json" --check
```

Install:

```shell
uv run --script --python 3.12 deploy/rhoai/install.py \
  --config "$HOME/.config/openshell-rhoai/config.json"
```

Set `namespace` in the JSON to your team/project namespace, or override it on the
command line (the override also works with `--check`):

```shell
uv run --script --python 3.12 deploy/rhoai/install.py \
  --config "$HOME/.config/openshell-rhoai/config.json" --namespace research-team
```

`--namespace` takes precedence; you may omit the JSON `namespace` when passing
this option. Both charts, Secrets, and sandbox resources target this namespace.
Gateway Service DNS and certificate SANs are derived from the selected name.
Existing namespace labels, annotations, and OpenShift SCC allocations are
preserved. Use a separate configuration with distinct public hostnames for each
gateway namespace, and register each dashboard callback with the IdP. Specifying
a different namespace creates another deployment; it does not move an existing one.

`--check` still downloads the pinned charts. It discards rendered manifests and
does not validate connectivity, SCC admission, token claims, or runtime behavior.
It reads the client secret file's existence/size, not its contents. Temporary
values files have owner-only permissions and contain no client or cookie secrets.

The installer creates the namespace only when missing, then creates an installer
state ConfigMap, a client/cookie Secret, and an optional OIDC CA ConfigMap.
It installs the gateway first, checks the gateway CA Secret,
and installs the dashboard with that CA and the internal gateway Service URL.
Both releases use OpenShift-assigned UID/GID settings. It waits for Helm readiness
and checks Route admission. Set `--timeout 15m` for a slower cluster.

## TLS and onboarding

The gateway uses the chart's PKI bootstrap hook and a passthrough Route. Generated
server SANs include the gateway public hostname and namespace-specific Service
names. This initial mode uses a private gateway CA: export and distribute its
`ca.crt` from `openshell-client-tls` through your trusted administrator process.
Register the gateway using the installed CLI's CA options from
`openshell gateway add --help`; do not disable certificate verification.
TLS client-certificate authentication is disabled for this OIDC-only deployment.
Supervisors retain gateway-minted sandbox JWT authentication.

The dashboard uses an edge Route and trusts the gateway CA for internal calls.
Its authentication proxy is the only exposed dashboard port. The OpenShift
router must use the `openshift-ingress` namespace network identity; host-network
router configurations require a separately reviewed chart configuration.

Configure a CLI OIDC registration separately with your IdP. After installation,
verify administrator and standard-user login. Administrators add workspace
memberships using the token's subject ID. Verify denied cross-workspace access,
sandbox creation/deletion, and terminal WebSockets before sharing the deployment.
Readiness and Route admission alone do not prove this flow works.

## Reruns, upgrades, and failures

Rerun the same command after correcting a failure. The administrator selects the
namespace; it does not need an installer ownership label. The fixed release names
provide one gateway/dashboard pair per namespace. An existing gateway chart under
a different release name, or either fixed release without installer state, is
rejected and needs explicit migration. Unmanaged gateway TLS/JWT Secrets are also
rejected so the bootstrap hook cannot silently reuse another gateway's identity.
Unrelated Helm releases in the selected namespace are left alone.

The original gateway hostname is recorded in the `openshell-installer` ConfigMap.
Earlier installations recorded it on the namespace; reruns migrate that state
without changing namespace metadata. Changing the recorded hostname is
rejected because the certificate hook preserves existing certificates; migrate
the certificate and endpoint explicitly rather than editing installer state.

The installer preserves existing client and cookie secrets. You may omit
`client_secret_file` after the first installation. A provided secret or private
OIDC CA that differs from the existing object causes an error; rotate credentials
explicitly and restart the consuming pods. Removing `ca_file` removes the mounts
on the next upgrade, but does not delete the old ConfigMap.

Change the pinned versions to upgrade. Both Helm releases receive the generated
values with `--reset-values`, so out-of-band Helm value edits are not preserved.
There is no cross-release transaction or automatic rollback: gateway success
followed by dashboard failure leaves the upgraded gateway in place. Failed
releases, namespaces, Secrets, and PVCs are preserved for diagnosis.

Inspect the two Helm releases, namespace pods/events, and the `openshell` and
`openshell-dashboard` Routes with the same explicit kubeconfig context. For
sandbox failures, inspect Agent Sandbox, SCC admission, storage, supervisor logs,
and NetworkPolicy enforcement. Namespace deletion is never part of this script.

## Image overrides

Optional `gateway.images` accepts `gateway`, `supervisor`, `sandboxRuntime`, and
`sandbox` objects. Optional `dashboard.images` accepts `dashboard` and `authProxy`.
Each object accepts `registry`, `repository`, `tag`, and `digest`, using the chart's
image semantics. For example:

```json
"images": {
  "gateway": {
    "registry": "quay.io",
    "repository": "your-org/openshell-gateway",
    "digest": "sha256:<verified-image-digest>"
  }
}
```

Pin compatible runtime and UI images as well as chart versions. The default
workload image uses a complete registry/repository path in `sandbox.repository`
(within `gateway.images.sandbox`); its chart does not use a separate registry key.
Image overrides do not establish FIPS qualification.

## Contributor validation

```shell
uv run --no-project --python 3.12 deploy/rhoai/test_install.py
```

For source-chart rendering, use `--check --gateway-chart-directory <path>
--dashboard-chart-directory <path>`. These overrides are rejected for installation.
Source charts carry version `0.0.0`, so this check renders images with the versions
from your configuration. The gateway source originally selected for this work is
`348a1fc62566892ba5530ac9f35acc983e4d9324`; a source commit is not a published OCI
chart version. Resolve it to the intended release before installing.
