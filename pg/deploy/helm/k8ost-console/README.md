# k8ost-console Helm chart

Deploys the [k8ost-console](../../../../docs/remote-control.md) — a web control
plane for CloudNativePG — with the image, RBAC scope, Build→Deploy grant, and
external exposure configurable. Default is `port-forward` (Kubernetes RBAC is the
login); optional Ingress / Gateway API routes are below.

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

## Exposing it on a DNS name

By default you `port-forward` and Kubernetes RBAC is the login. To put it on a
hostname, add a route and terminate TLS at your gateway:

```bash
# Gateway API
--set gatewayRoute.enabled=true --set gatewayRoute.host=k8ost.example.com \
--set 'gatewayRoute.parentRefs[0].name=web' --set 'gatewayRoute.parentRefs[0].namespace=infra'

# or Ingress
--set ingress.enabled=true --set ingress.className=<class> \
--set ingress.host=k8ost.example.com --set ingress.tls.secretName=<cert>
```

The console speaks plain HTTP behind the gateway; its SSE stream needs the gateway
not to buffer.

**Auth.** The console has no login of its own, so an exposed Ingress needs one of:

- built-in basic-auth — `--set console.basicAuth.enabled=true --set console.basicAuth.username=admin --set console.basicAuth.password=<pass>` (works on any path; shared credential, so interim not SSO);
- your controller's auth annotation, or an OIDC forward-auth proxy for per-user SSO.

The chart won't render an unauthenticated Ingress unless you set `ingress.insecureNoAuth=true`
(for a private-network-only deployment). TLS is still required — the basic-auth
credential rides every request.

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
