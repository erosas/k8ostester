# CNPG / Kubernetes gotchas

Non-obvious traps we hit building and testing this, and how each is handled. Most
were caught by running the real thing (the testbed golden path, the `--az` run, a
live cluster) rather than by reading docs — so they're worth writing down.

## Placement & scheduling

- **Single-instance PodDisruptionBudget deadlock.** CNPG's default PDB (`minAvailable: 1`)
  with no standby allows *zero* disruptions, so a node drain blocks forever — the node
  can never be cordoned without deleting the pod. The builder sets `enablePDB: false`
  for a 1-instance cluster.
- **Pooler pods share the cluster label.** Both the PgBouncer pooler pods and the
  Postgres instance pods carry `cnpg.io/cluster: <name>`. Any selector meant for
  instances only — `topologySpreadConstraints`, an "all pods on the new image?" upgrade
  check — silently also matches the poolers, so the scheduler balances instances +
  poolers together (e.g. lands 2 instances in one AZ) and upgrade polls never satisfy.
  Always scope to `cnpg.io/podRole: instance`.
- **Instance zone is pinned by its PVC.** Deleting an instance pod doesn't move it to
  another zone — its data volume is bound to the original zone, so it reschedules there.
  Zone spread is decided at *initial provisioning*; fixing a spread constraint requires
  a fresh cluster, not a pod restart.

## Backups (Barman Cloud plugin)

- **The plugin needs cert-manager installed *first*.** The Barman Cloud plugin ships
  cert-manager `Certificate` CRs for its sidecar TLS. Apply the plugin before
  cert-manager exists and those CRs are silently skipped (unknown kind); the plugin pod
  then hangs forever on missing TLS secrets. Order: cert-manager → wait Ready → plugin.
- **`serverName` lives on the Cluster, not the ObjectStore.** The `ObjectStore` CR
  *rejects* `serverName` in `spec.configuration` — it must be passed as a plugin
  *parameter* on the Cluster (`spec.plugins[].parameters.serverName`), and again in the
  recovery `externalClusters[].plugin.parameters` so a restore reads the right WAL.
- **minimal / standard images don't bundle barman-cloud.** Only the deprecated `system`
  flavor bundles it. On `minimal`/`standard` (the recommended flavors), backups *require*
  the plugin (a sidecar) — the in-tree `spec.backup.barmanObjectStore` won't work.

## Operations & verification

- **PITR overshoots by a few rows under a live writer.** A recovery target captured with
  `now()` is racy — transactions commit within the same instant and are legitimately
  recovered — so an exact `restored == target` check fails. Assert a *window*:
  `target ≤ restored ≤ (origin head at restore time)`; that still proves it landed at the
  point and not at latest.
- **Don't compare a cumulative counter across a pod restart.** A rotation restarts the
  app pods, which resets any in-pod counter to 0, so "ops after > ops before" is wrong.
  Measure a *live delta* on one current pod instead ("is it serving now?").
- **`.spec.probes` needs CNPG ≥ 1.26.** 1.24 rejects the block outright (verified live).
  The manifests target 1.26+.
- **"Archiving LIVE" needs positive proof.** The topology badge must key on the
  `ContinuousArchiving` condition being *True* (or WAL actually archived) — not on the
  condition merely being absent, which would show LIVE for a cluster whose object store
  isn't working at all.

## Console, images & CI

- **A single failed discovery poll shouldn't flash an error.** Pods mid-reconcile / brief
  API blips make one poll fail and self-heal ~2s later; the banner is debounced to only
  show after 2 consecutive misses.
- **Pod exec/log output isn't guaranteed UTF-8.** Decoding it can throw (surfacing as a
  cryptic "decode" error) when a pod is restarting; decode with `errors="replace"`.
- **The free Chainguard wolfi-base apk index lags.** A pinned base digest can serve an
  outdated package (e.g. an unpatched `python-3.14`) and fail the Trivy scan even though
  a fix exists upstream. Bump the base digest to one whose apk index carries the patched
  package, rather than suppressing the CVE.
