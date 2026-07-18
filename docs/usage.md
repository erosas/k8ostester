# Usage — the two scenarios

k8ost is used two ways. Pick the one that matches what you have; you don't need
to read both.

- **[Scenario 1 — deploy a config, then session-test it](#scenario-1--deploy-a-config-and-test-it)**
  You have a cluster + operator and a *config you want to validate*. k8ost
  deploys it into a throwaway namespace, drives load, and hands you interactive
  chaos controls.
- **[Scenario 2 — attach to an existing DB and break it](#scenario-2--attach-to-an-existing-database)**
  An app already deployed the database. You attach k8ost as a chaos control
  plane and inject faults while the app runs, to prove the app survives them.
  k8ost deploys nothing and, on exit, removes only its own artifacts.

Both assume: a reachable **kubeconfig** (the current context), and — for the
Postgres scenarios — the **CloudNativePG operator already installed** (`k8ost
env check` confirms it).

---

## Getting the tool

Two options; the container needs nothing but Docker.

**Container (recommended):** the `k8ost-docker` shim runs the tool image with
your kubeconfig and current directory mounted, and gives the TUI a terminal.

```bash
docker pull bytestream89/k8os-tester:0.1.2
export K8OST_TOOL_IMAGE=bytestream89/k8os-tester:0.1.2
export K8OST_LOADGEN_IMAGE=bytestream89/k8os-loadgen:0.1.2   # load pods use this
curl -fsSLO https://raw.githubusercontent.com/erosas/k8ostester/main/k8ost-docker && chmod +x k8ost-docker

./k8ost-docker env check
```

**Local install** (if you'd rather run natively; needs Python ≥ 3.14, `kubectl`,
`helm`): `uv tool install --editable ./k8ostester-core`, then use `k8ost …`
directly instead of `./k8ost-docker …`.

> **Remote clusters with exec-plugin auth (EKS/GKE/AKS):** the tool image has no
> `aws`/`gcloud`/`az` binary. Either extend the image (`apk add aws-cli`) or use
> the local install on a host that already has the CLI.

### Pulling images through Artifactory (or any proxy)

Two images are pulled by **two different machines** — point each accordingly:

| Image | Pulled by | Set | Auth |
| --- | --- | --- | --- |
| tool (`k8os-tester`) | your laptop's Docker | `K8OST_TOOL_IMAGE=your-art/…/k8os-tester:0.1.2` | `docker login your-art` |
| loadgen (`k8os-loadgen`) | the **cluster's** kubelet | `K8OST_LOADGEN_IMAGE=your-art/…/k8os-loadgen:0.1.2` | see below |

The loadgen image is pulled **by your cluster nodes**, so laptop `docker login`
doesn't help:

- **Scenario 1 (deploy):** k8ost creates a fresh random namespace per run, so a
  pull secret can't be pre-placed there — give the **nodes' container runtime**
  credentials to your registry (a cluster-wide default pull secret or node
  config). Then no per-experiment secret is needed.
- **Scenario 2 (attach):** the app's namespace already exists — create a
  `docker-registry` secret there once and name it via `load.pull_secret`
  (see the Scenario 2 config below).

Other images (Wolfi base, postgres image, SeaweedFS, OTEL) are overridable too —
see the README "Running through a mirror" section.

---

## Scenario 1 — deploy a config and test it

**You have:** a cluster + operator, and a config you want to prove out.

The config is a plain CNPG manifest. The **reference experiment**
`experiments/postgres-cnpg/20-cnpg-reference` is the canonical starting point —
3-instance quorum sync, backup + WAL + PITR, periodic base backups, and rw + ro
poolers, every block commented with *why*. Copy it and edit `manifests/` to your
config.

```bash
git clone https://github.com/erosas/k8ostester && cd k8ostester

# see what the cluster supports (nodes/zones, storage, snapshot classes, operators)
./k8ost-docker env check --context my-remote-context

# interactive lab against the reference config (deploys → drives load → your controls)
./k8ost-docker session experiments/postgres-cnpg/20-cnpg-reference \
    --context my-remote-context --pods 1 --rate 20
```

k8ost deploys the manifest into a throwaway namespace, starts a load pod, and
drops you into the dashboard. Controls:

| Key | Action |
| --- | --- |
| `+` / `-` | scale the load pool by one pod |
| `[` / `]` | change ops/s per pod |
| target dropdown | pick primary / a replica / a specific instance |
| `k` | kill the selected target (pod_kill) |
| `p` | partition the target for 30s |
| tech-ops row | `base backup`, then `restore (PITR)` to a point in the window |
| `q` | stop, collect artifacts, tear the namespace down (`--keep` leaves it up) |

Everything you do is recorded to a replayable `experiment.yaml` under
`results/…/recorded/` — run that later as a verdict-producing regression test.

**Prefer a fixed, scripted run with a pass/fail verdict** (CI, not a lab)? Use
`run` instead of `session` — same config, but it executes the experiment.yaml's
fault timeline and evaluates the goals:

```bash
./k8ost-docker run experiments/postgres-cnpg/20-cnpg-reference --context my-remote-context --view plain
```

---

## Scenario 2 — attach to an existing database

**You have:** an app that already deployed its database (with the config you
settled on), running in some namespace. You want to prove the app tolerates
failures — without k8ost deploying or deleting anything.

```bash
export K8OST_TOOL_IMAGE=bytestream89/k8os-tester:0.1.2
./k8ost-docker session --attach my-app-namespace --context my-remote-context
```

What's different from Scenario 1:

- **No manifest, no deploy.** k8ost discovers the existing cluster in that
  namespace (auto-detects the technology; force with `--technology postgres-cnpg`).
- **`--pods` defaults to 0** — the *app* is the load. You inject faults while the
  app's real traffic runs and watch whether it stays healthy. Add `--pods 1` if
  you also want synthetic load alongside.
- **Teardown removes only k8ost's own artifacts — never the app's namespace or
  data.** That safety guarantee is what makes it OK to run against a live,
  app-owned database.

Then use the same controls — target the primary, hit `k` or `p`, and watch the
app's error rate and recovery on the dashboard. Does its connection pool ride
the failover, or hang? That's the proof.

If the loadgen image comes from a private registry and you added `--pods`,
pre-create the pull secret in the app namespace and name it:

```bash
kubectl -n my-app-namespace create secret docker-registry art-creds \
  --docker-server=your-art.jfrog.io --docker-username=… --docker-password=…
```
```yaml
# in a minimal experiment.yaml you pass instead of --attach, or via defaults:
load:
  image: your-art.jfrog.io/…/k8os-loadgen:0.1.2
  pull_secret: art-creds
```

---

## Backup, WAL & PITR — how the reference config does it

PITR = **a base backup (the anchor) + continuous WAL archiving (the deltas)**;
a restore replays WAL forward from a base to your target time. In the reference
config:

- **WAL archiving** starts the instant `spec.backup.barmanObjectStore` exists —
  continuous, no schedule. This is what allows PITR to *any* second.
- **Periodic base backups** come from the `ScheduledBackup` CR (daily here).
  Without a fresh anchor, a restore replays ever-more WAL and storage grows — so
  base cadence bounds recovery time; go to every few hours for high-write DBs.
- **`retentionPolicy: 7d`** makes Barman prune old base backups *and* their
  orphaned WAL — bounding both rewind distance and storage.
- **The bucket must already exist.** The operator writes objects into it but
  never creates it. (Against SeaweedFS, k8ost creates the `backups` bucket for
  you; against real S3 you provision the bucket + IAM yourself.)
- **VolumeSnapshot** is an alternative *anchor* (`method: volumeSnapshot`, needs
  a CSI snapshot class) for large volumes — but you **still** need WAL archiving
  for the deltas between snapshots.

> **Operator-version note:** these experiments use the inline
> `spec.backup.barmanObjectStore` API. CNPG 1.26+ deprecates it in favor of the
> **Barman Cloud Plugin** (same ScheduledBackup / retention / pre-existing-bucket
> model, configured through a plugin object). If your operator expects the
> plugin, that's a separate migration — the concepts above are identical.
