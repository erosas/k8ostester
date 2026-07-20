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
| `resources` | 50m/96Mi → 500m/256Mi | |

## Uninstall

```bash
helm uninstall console -n db
```

The raw manifests in [`pg/deploy/`](..) (`console.yaml`, `rbac-clusterwide.yaml`,
`console-lab.yaml`) remain as a Helm-free alternative.
