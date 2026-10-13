#!/usr/bin/env -S uv run --script
# SPDX-FileCopyrightText: Copyright (c) 2026 OpenShell contributors
# SPDX-License-Identifier: Apache-2.0
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install the gateway and dashboard as separate Helm releases on OpenShift."""

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

OWNER = "openshell.ai/installer"
OWNER_VALUE = "odh-dashboard"
GATEWAY_HOST = "openshell.ai/installer-gateway-host"
OIDC_SECRET = "openshell-dashboard-oidc"
OIDC_CA = "openshell-oidc-ca"
VERSION = re.compile(r"0\.1\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
DNS = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fields(obj, required, optional=()):
    require(isinstance(obj, dict), "Configuration sections must be JSON objects")
    require(
        not (set(obj) - set(required) - set(optional)),
        f"Unknown configuration fields: {set(obj) - set(required) - set(optional)}",
    )
    for key in required:
        require(key in obj and obj[key] != "", f"Missing configuration field: {key}")


def load_config(path):
    config = json.loads(path.read_text())
    fields(
        config,
        ("namespace", "context", "gateway", "dashboard", "oidc"),
        ("agent_sandbox_namespace", "agent_sandbox_deployment"),
    )
    ns = config["namespace"]
    require(
        isinstance(ns, str)
        and len(ns) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", ns),
        "Invalid namespace",
    )
    require(
        isinstance(config["context"], str) and config["context"].strip(),
        "context is required",
    )
    for name in ("gateway", "dashboard"):
        item = config[name]
        fields(item, ("chart", "version", "host"), ("images",))
        require(
            isinstance(item["chart"], str) and item["chart"].startswith("oci://"),
            f"{name}.chart must be an OCI chart reference",
        )
        require(
            isinstance(item["version"], str) and VERSION.fullmatch(item["version"]),
            f"{name}.version must pin a 0.1.x release (no latest or version ranges)",
        )
        host = item["host"]
        require(
            isinstance(host, str)
            and len(host) <= 253
            and DNS.fullmatch(host)
            and all(
                0 < len(label) <= 63
                and not label.startswith("-")
                and not label.endswith("-")
                for label in host.split(".")
            ),
            f"Invalid {name}.host",
        )
        components = (
            ("gateway", "supervisor", "sandboxRuntime", "sandbox")
            if name == "gateway"
            else ("dashboard", "authProxy")
        )
        fields(item.get("images", {}), (), components)
        for component, image in item.get("images", {}).items():
            keys = (
                ("repository", "tag", "digest")
                if component == "sandbox"
                else ("registry", "repository", "tag", "digest")
            )
            fields(image, (), keys)
            require(
                all(isinstance(value, str) for value in image.values()),
                "Image values must be strings",
            )
    require(
        config["gateway"]["host"] != config["dashboard"]["host"],
        "Gateway and dashboard need distinct hosts",
    )
    oidc = config["oidc"]
    fields(
        oidc,
        ("issuer", "client_id", "audience", "roles_claim", "admin_role", "user_role"),
        (
            "provider",
            "allowed_groups",
            "allowed_roles",
            "client_secret_file",
            "ca_file",
        ),
    )
    for key in (
        "issuer",
        "client_id",
        "audience",
        "roles_claim",
        "admin_role",
        "user_role",
    ):
        require(
            isinstance(oidc[key], str) and oidc[key].strip(),
            f"oidc.{key} must be a nonempty string",
        )
    issuer = urlsplit(oidc["issuer"])
    require(
        issuer.scheme == "https"
        and issuer.hostname
        and not issuer.username
        and not issuer.password
        and not issuer.query
        and not issuer.fragment,
        "OIDC issuer must be an HTTPS URL",
    )
    require(
        oidc["client_id"] != oidc["audience"],
        "Browser client ID and gateway audience must be distinct",
    )
    require(
        oidc["admin_role"] != oidc["user_role"], "Admin and user roles must be distinct"
    )
    require(
        oidc.get("provider", "oidc") in ("oidc", "keycloak-oidc"),
        "Unsupported OIDC provider",
    )
    for key in ("allowed_groups", "allowed_roles"):
        require(
            isinstance(oidc.get(key, []), list)
            and all(isinstance(v, str) and v for v in oidc.get(key, [])),
            f"oidc.{key} must be a list of strings",
        )
    require(
        not oidc.get("allowed_roles") or oidc.get("provider") == "keycloak-oidc",
        "allowed_roles requires keycloak-oidc; use allowed_groups for generic OIDC",
    )
    for key in ("client_secret_file", "ca_file"):
        if oidc.get(key):
            source = (path.parent / oidc[key]).resolve()
            require(
                source.is_file() and source.stat().st_size > 0,
                f"Missing or empty oidc.{key}",
            )
            oidc[key] = str(source)
    return config


def chart_values(config):
    ns, oidc = config["namespace"], config["oidc"]
    ca = OIDC_CA if oidc.get("ca_file") else ""
    security = {"runAsUser": None, "seccompProfile": {"type": "RuntimeDefault"}}
    gateway = {
        "fullnameOverride": "openshell",
        "podSecurityContext": None,
        "securityContext": security,
        "replicaCount": 1,
        "server": {
            "disableTls": False,
            "sandboxNamespace": ns,
            "auth": {"allowUnauthenticatedUsers": False},
            "tls": {
                "enableMtls": False,
                "clientCaSecretName": "",
                "certSecretName": "openshell-server-tls",
                "clientTlsSecretName": "openshell-client-tls",
            },
            "oidc": {
                "issuer": oidc["issuer"],
                "audience": oidc["audience"],
                "rolesClaim": oidc["roles_claim"],
                "adminRole": oidc["admin_role"],
                "userRole": oidc["user_role"],
                "caConfigMapName": ca,
            },
        },
        "pkiInitJob": {
            "enabled": True,
            "serverDnsNames": [
                config["gateway"]["host"],
                f"openshell.{ns}.svc",
                f"openshell.{ns}.svc.cluster.local",
            ],
        },
        "openshiftRoute": {"enabled": True, "host": config["gateway"]["host"]},
    }
    dashboard = {
        "fullnameOverride": "openshell-dashboard",
        "podSecurityContext": None,
        "securityContext": security,
        "gateway": {
            "url": f"grpcs://openshell.{ns}.svc.cluster.local:8080",
            "caSecretName": "openshell-client-tls",
        },
        "oidc": {
            "provider": oidc.get("provider", "oidc"),
            "issuer": oidc["issuer"],
            "clientId": oidc["client_id"],
            "existingSecret": OIDC_SECRET,
            "audience": oidc["audience"],
            "rolesClaim": oidc["roles_claim"],
            "adminRole": oidc["admin_role"],
            "codeChallengeMethod": "S256",
            "allowedGroups": oidc.get("allowed_groups", []),
            "allowedRoles": oidc.get("allowed_roles", []),
            "caConfigMapName": ca,
        },
        "openshiftRoute": {
            "enabled": True,
            "host": config["dashboard"]["host"],
            "termination": "edge",
        },
        "networkPolicy": {"ingressNamespace": "openshift-ingress"},
    }
    for name, values in (("gateway", gateway), ("dashboard", dashboard)):
        for component, image in config[name].get("images", {}).items():
            values.setdefault(component, {})["image"] = image
    return gateway, dashboard


class Installer:
    def __init__(self, config, timeout):
        self.config = config
        self.timeout = timeout
        self.kube = ["kubectl", "--context", config["context"], "--request-timeout=30s"]
        self.ns = config["namespace"]

    def run(self, args, payload=None):
        # Never log stdin: it can contain Secret data. Do not echo commands.
        result = subprocess.run(
            args, input=payload, text=True, capture_output=True, check=False
        )
        if result.returncode:
            raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def get(self, kind, name, namespaced=True):
        args = self.kube + (["-n", self.ns] if namespaced else [])
        output = self.run(
            [*args, "get", kind, name, "--ignore-not-found", "-o", "json"]
        )
        return json.loads(output) if output.strip() else None

    def create(self, resource):
        self.run([*self.kube, "create", "-f", "-"], json.dumps(resource))

    def preflight(self):
        self.run([*self.kube, "get", "--raw", "/apis/route.openshift.io/v1"])
        crd = self.get("crd", "sandboxes.agents.x-k8s.io", False)
        require(
            crd
            and any(
                v["served"] and v["name"] in ("v1alpha1", "v1beta1")
                for v in crd["spec"]["versions"]
            ),
            "Install the Agent Sandbox CRDs and controller first",
        )
        self.run(
            [
                *self.kube,
                "-n",
                self.config.get("agent_sandbox_namespace", "agent-sandbox-system"),
                "rollout",
                "status",
                "deployment/"
                + self.config.get(
                    "agent_sandbox_deployment", "agent-sandbox-controller"
                ),
                "--timeout",
                self.timeout,
            ]
        )
        namespace = self.get("namespace", self.ns, False)
        require(
            not namespace
            or namespace["metadata"].get("labels", {}).get(OWNER) == OWNER_VALUE,
            "Namespace already exists and is not owned by this installer; choose a new namespace",
        )
        require(
            not namespace or namespace.get("status", {}).get("phase") != "Terminating",
            "Namespace is terminating",
        )
        require(
            not namespace
            or namespace["metadata"].get("annotations", {}).get(GATEWAY_HOST)
            == self.config["gateway"]["host"],
            "Gateway hostname differs from the original install; certificate migration is required",
        )
        existing = self.get("secret", OIDC_SECRET) if namespace else None
        require(
            existing or self.config["oidc"].get("client_secret_file"),
            "oidc.client_secret_file is required for the first install",
        )
        return namespace

    def credentials(self):
        oidc = self.config["oidc"]
        existing = self.get("secret", OIDC_SECRET)
        supplied = (
            Path(oidc["client_secret_file"]).read_bytes().rstrip(b"\r\n")
            if oidc.get("client_secret_file")
            else None
        )
        if supplied is not None:
            require(supplied, "Client secret file contains no secret")
        if existing:
            data = existing.get("data", {})
            require(
                data.get("client-secret") and data.get("cookie-secret"),
                "Existing OIDC Secret is missing required keys",
            )
            require(
                supplied is None or base64.b64decode(data["client-secret"]) == supplied,
                "OIDC Secret differs from file; rotate it explicitly before rerunning",
            )
        else:
            require(supplied, "Client secret file is required")
            self.create(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "type": "Opaque",
                    "metadata": {"name": OIDC_SECRET, "namespace": self.ns},
                    "data": {
                        "client-secret": base64.b64encode(supplied).decode(),
                        "cookie-secret": base64.b64encode(
                            secrets.token_urlsafe(32).encode()
                        ).decode(),
                    },
                }
            )
        if oidc.get("ca_file"):
            ca = Path(oidc["ca_file"]).read_text()
            existing_ca = self.get("configmap", OIDC_CA)
            require(
                not existing_ca or existing_ca.get("data", {}).get("ca.crt") == ca,
                "OIDC CA differs; rotate the ConfigMap and restart both releases explicitly",
            )
            if not existing_ca:
                self.create(
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": OIDC_CA, "namespace": self.ns},
                        "data": {"ca.crt": ca},
                    }
                )

    def wait_for_route(self, release):
        seconds = (
            int(self.timeout[:-1]) * {"s": 1, "m": 60, "h": 3600}[self.timeout[-1]]
        )
        deadline = time.monotonic() + seconds
        while True:
            route = self.get("route", release)
            if route and any(
                condition.get("type") == "Admitted"
                and condition.get("status") == "True"
                for ingress in route.get("status", {}).get("ingress", [])
                for condition in ingress.get("conditions", [])
            ):
                return
            require(
                time.monotonic() < deadline,
                f"Route {release} is not admitted; inspect it and rerun",
            )
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def install(self, charts, values):
        namespace = self.preflight()
        if not namespace:
            self.create(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": self.ns,
                        "labels": {OWNER: OWNER_VALUE},
                        "annotations": {GATEWAY_HOST: self.config["gateway"]["host"]},
                    },
                }
            )
        self.credentials()
        for index, release in enumerate(("openshell", "openshell-dashboard")):
            print(f"Installing {release} in {self.ns}...", flush=True)
            self.run(
                [
                    "helm",
                    "upgrade",
                    "--install",
                    release,
                    str(charts[index]),
                    "--kube-context",
                    self.config["context"],
                    "--namespace",
                    self.ns,
                    "--values",
                    str(values[index]),
                    "--reset-values",
                    "--wait",
                    "--wait-for-jobs",
                    "--timeout",
                    self.timeout,
                    "--history-max",
                    "5",
                ]
            )
            if index == 0:
                ca = self.get("secret", "openshell-client-tls")
                require(
                    ca and ca.get("data", {}).get("ca.crt"),
                    "Gateway did not create its CA Secret",
                )
            self.wait_for_route(release)
        print(f"Dashboard: https://{self.config['dashboard']['host']}")
        print(f"Gateway: https://{self.config['gateway']['host']}")
        print(
            "Register the gateway with the CLI using its CA, then log in through customer OIDC."
        )
        print(
            "An administrator must add users to workspaces. Verify login, denied access, sandbox creation and terminals."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and render charts without accessing the cluster",
    )
    parser.add_argument(
        "--gateway-chart-directory",
        type=Path,
        help="Local source chart for --check only",
    )
    parser.add_argument(
        "--dashboard-chart-directory",
        type=Path,
        help="Local source chart for --check only",
    )
    parser.add_argument(
        "--timeout", default="10m", help="Helm/controller wait timeout (default: 10m)"
    )
    args = parser.parse_args()
    require(
        args.check
        or not (args.gateway_chart_directory or args.dashboard_chart_directory),
        "Local source charts are only supported with --check; install versioned OCI artifacts",
    )
    require(
        re.fullmatch(r"[1-9][0-9]*[smh]", args.timeout),
        "Timeout must be a positive duration such as 10m",
    )
    for tool in ["helm"] if args.check else ["helm", "kubectl"]:
        require(shutil.which(tool), f"Required tool not found: {tool}")
    config = load_config(args.config.resolve())
    installer = Installer(config, args.timeout)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="openshell-install-") as temporary:
        root = Path(temporary)
        charts, files = [], []
        for index, (name, values, local) in enumerate(
            zip(
                ("gateway", "dashboard"),
                chart_values(config),
                (args.gateway_chart_directory, args.dashboard_chart_directory),
                strict=True,
            )
        ):
            if local:
                chart = local.resolve()
                require((chart / "Chart.yaml").is_file(), f"Missing chart: {chart}")
                # Source charts carry 0.0.0; render with the requested release image tag.
                values.setdefault("global", {})["image"] = {
                    "tag": config[name]["version"]
                }
            else:
                destination = root / name
                destination.mkdir()
                installer.run(
                    [
                        "helm",
                        "pull",
                        config[name]["chart"],
                        "--version",
                        config[name]["version"],
                        "--destination",
                        str(destination),
                    ]
                )
                packages = list(destination.glob("*.tgz"))
                require(len(packages) == 1, f"Expected one downloaded {name} chart")
                chart = packages[0]
            value_file = root / f"{name}.json"
            value_file.write_text(json.dumps(values))
            release = ("openshell", "openshell-dashboard")[index]
            installer.run(
                [
                    "helm",
                    "template",
                    release,
                    str(chart),
                    "--namespace",
                    config["namespace"],
                    "--values",
                    str(value_file),
                    "--api-versions",
                    "route.openshift.io/v1",
                    "--api-versions",
                    "agents.x-k8s.io/v1beta1",
                ]
            )
            charts.append(chart)
            files.append(value_file)
        print(
            "Both charts rendered successfully. This does not verify cluster or API compatibility.",
            flush=True,
        )
        if not args.check:
            installer.install(charts, files)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        print(
            f"Installation stopped: {error}\nCluster resources were not rolled back or deleted.",
            file=sys.stderr,
        )
        sys.exit(1)
