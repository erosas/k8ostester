# k8ost-console Helm chart

Deploys the [k8ost-console](../../../../docs/remote-control.md) — a web control
plane for CloudNativePG — into a cluster, with the image location, RBAC scope,
and Build→Deploy grant configurable. There is **no ingress**: you reach it over
`kubectl port-forward`, which already requires Kubernetes RBAC and is the auth gate.

## Install

```bash
# namespaced: operate CNPG clusters in the release namespace
helm install console pg/deploy/helm/k8ost-console -n db --create-namespace

# reach it (port-forward IS the login)
kubectl -n db port-forward svc/console-k8ost-console 8700:8700
#  → http://127.0.0.1:8700
```

Fleet-wide (operate clusters in **every** namespace, read nodes for zone info):

```bash
helm install console pg/deploy/helm/k8ost-console -n ops --create-namespace \
  --set rbac.scope=cluster
```

Enable the Builder's **Deploy** (adds create rights for clusters/secrets/RBAC/monitoring):

```bash
helm install console pg/deploy/helm/k8ost-console -n db --set rbac.lab=true
```

## Custom image location / tag (proxy or mirror)

Behind an Artifactory/Nexus proxy, mirror `bytestream89/k8os-console` and point the
chart at the mirror. Everything about the image is a value:

```bash
helm install console pg/deploy/helm/k8ost-console -n db \
  --set image.repository=registry.internal/mirror/k8os-console \
  --set image.tag=0.2.0 \
  --set image.pullPolicy=IfNotPresent \
  --set 'imagePullSecrets[0].name=registry-cred'      # kubernetes.io/dockerconfigjson secret you created
```

`image.tag` defaults to the chart's `appVersion`, so it tracks the chart unless you pin it.

## Exposing it on a DNS name (and auth)

By default there's **no external exposure** — you `port-forward`, and Kubernetes
RBAC is the login. To reach the console on a hostname you go through your **gateway
controller**, which terminates TLS and routes the hostname to the Service.

**How it fits together.** DNS name → gateway controller (TLS termination + auth) →
the console's ClusterIP Service (plain HTTP) → the pod. The chart only creates the
route (the `HTTPRoute` or `Ingress`) that hands your hostname to the Service; the
gateway controller and its certificate/auth are yours.

**Auth is not optional on a DNS name.** By itself the console has no login, so
exposed it's a mutating control plane open to anyone who can reach it. Pick one:

### Built-in basic-auth (simplest, controller-agnostic)

The console can check basic-auth **itself**, so it protects every path the same way
whether you reach it via port-forward, Ingress, or a gateway — no controller-specific
config. It's a shared credential (interim, not SSO), and TLS must still terminate at
the edge since the credential rides each request. Off by default ("no auth for now"):

```bash
helm install console pg/deploy/helm/k8ost-console -n db \
  --set console.basicAuth.enabled=true \
  --set console.basicAuth.username=admin \
  --set console.basicAuth.password=<a-strong-password>
# combine with ingress.* or gatewayRoute.* below to put it on a DNS name
```

The chart stores the credential in a Secret and passes it as `K8OST_BASIC_AUTH`.

### Delegate auth to the gateway

Alternatively enforce auth at the edge — your controller's auth extension, or an OIDC
forward-auth proxy (per-user SSO). Then the console needs no built-in auth. The chart
**refuses** to render an Ingress with neither `console.basicAuth` on, an auth
annotation, nor `ingress.insecureNoAuth=true` (the last is for "behind a private
network / VPN only", i.e. the network is the gate).

### Gateway API (vendor-neutral)

Attach an `HTTPRoute` to an existing `Gateway` that owns the TLS listener and enforces
auth (via your controller's auth extension / an external-auth policy on the Gateway):

```bash
helm install console pg/deploy/helm/k8ost-console -n db \
  --set gatewayRoute.enabled=true --set gatewayRoute.host=k8ost.example.com \
  --set 'gatewayRoute.parentRefs[0].name=web' --set 'gatewayRoute.parentRefs[0].namespace=infra'
```

### Ingress (if you run an Ingress controller instead)

```bash
helm install console pg/deploy/helm/k8ost-console -n db \
  --set ingress.enabled=true --set ingress.className=<your-class> \
  --set ingress.host=k8ost.example.com --set ingress.tls.secretName=k8ost-tls \
  --set 'ingress.annotations.<your-controllers-auth-annotation>=<value>'
```

TLS terminates at the gateway; the console speaks plain HTTP behind it, and its
`/api/stream` Server-Sent Events work as long as the gateway doesn't buffer responses.

## Values

| Key | Default | What |
| --- | --- | --- |
| `image.repository` | `bytestream89/k8os-console` | image path — set to your mirror |
| `image.tag` | `""` (→ `appVersion`) | image tag |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | pull secrets for a private/proxy registry |
| `rbac.scope` | `namespaced` | `namespaced` (Role) or `cluster` (ClusterRole + node read) |
| `rbac.lab` | `false` | add create rights for the Builder's Deploy |
| `service.type` | `ClusterIP` | keep ClusterIP — port-forward is the entry point |
| `service.port` | `8700` | |
| `console.namespace` | `""` | scope the console to one namespace (default: its own) |
| `console.cluster` | `""` | pre-select a cluster |
| `console.grafana` | `""` | Grafana base URL for metric deep-links |
| `console.target` | `""` | a PG image/version to offer as an upgrade |
| `console.extraArgs` | `[]` | extra `k8ost-console` flags |
| `console.basicAuth.enabled` | `false` | built-in basic-auth (controller-agnostic) |
| `console.basicAuth.username` / `.password` | `""` | the shared credential (stored in a Secret) |
| `resources` | 50m/96Mi → 500m/256Mi | |
| `gatewayRoute.enabled` | `false` | expose via a Gateway API `HTTPRoute` |
| `gatewayRoute.parentRefs` | `[]` | the `Gateway`(s) to attach to |
| `gatewayRoute.host` | `""` | the DNS name |
| `ingress.enabled` | `false` | expose via an Ingress on `ingress.host` |
| `ingress.className` | `""` | your ingress class |
| `ingress.host` | `""` | the DNS name (required when enabled) |
| `ingress.tls.secretName` | `""` | cert secret; `""` = the controller default cert |
| `ingress.annotations` | `{}` | where you attach your controller's auth |
| `ingress.insecureNoAuth` | `false` | escape hatch to expose with no auth (don't) |

## Uninstall

```bash
helm uninstall console -n db
```

The raw manifests in [`pg/deploy/`](..) (`console.yaml`, `rbac-clusterwide.yaml`,
`console-lab.yaml`) remain as a Helm-free alternative.
