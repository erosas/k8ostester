"""Kubernetes access for a chosen kubeconfig context.

Everything in the framework talks to the cluster through a ClusterClient so that
experiments can target any context (local or remote) without global state.
Manifest application shells out to kubectl (server-side apply handles arbitrary
kinds, including CRs, without us re-implementing apply semantics).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import TypeVar

from kubernetes import client, config

from k8ostester_kernel.exceptions import K8osInfraError

T = TypeVar("T")


def wait_until(
    check: Callable[[], T],
    timeout: float,
    interval: float = 2.0,
    desc: str | Callable[[], str] = "condition not met",
) -> T:
    """Poll `check` until it returns a truthy value, which is then returned.

    Deadlines use time.monotonic: wall-clock deadlines get bankrupted when the
    machine sleeps mid-wait (two runs were marked error by exactly this).
    On timeout raises TimeoutError from `desc` (a string or a callable, for
    messages that need the last observed state)."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            msg = desc() if callable(desc) else desc
            raise TimeoutError(f"{msg} after {timeout}s")
        time.sleep(interval)


class ClusterClient:
    def __init__(self, context: str | None = None) -> None:
        self.context = context
        self._api_client = config.new_client_from_config(context=context)

    @cached_property
    def core(self) -> client.CoreV1Api:
        return client.CoreV1Api(self._api_client)

    @cached_property
    def apps(self) -> client.AppsV1Api:
        return client.AppsV1Api(self._api_client)

    @cached_property
    def storage(self) -> client.StorageV1Api:
        return client.StorageV1Api(self._api_client)

    @cached_property
    def apiext(self) -> client.ApiextensionsV1Api:
        return client.ApiextensionsV1Api(self._api_client)

    @cached_property
    def custom(self) -> client.CustomObjectsApi:
        return client.CustomObjectsApi(self._api_client)

    @cached_property
    def batch(self) -> client.BatchV1Api:
        return client.BatchV1Api(self._api_client)

    @cached_property
    def version(self) -> client.VersionApi:
        return client.VersionApi(self._api_client)

    @cached_property
    def networking(self) -> client.NetworkingV1Api:
        return client.NetworkingV1Api(self._api_client)

    def has_crd(self, name: str) -> bool:
        """True if a CustomResourceDefinition with this full name exists."""
        try:
            self.apiext.read_custom_resource_definition(name)
            return True
        except client.ApiException as e:
            if e.status == 404:
                return False
            raise

    # -- namespace lifecycle -------------------------------------------------

    def create_namespace(self, name: str, labels: dict[str, str] | None = None) -> None:
        body = client.V1Namespace(
            metadata=client.V1ObjectMeta(name=name, labels=labels or {})
        )
        self.core.create_namespace(body)

    def set_namespace_labels(self, name: str, labels: dict[str, str | None]) -> None:
        """Patch namespace labels (a value of None removes the label). Used to
        make an attached namespace discoverable by the concurrent-run guard
        without owning it — the labels are reverted at teardown."""
        self.core.patch_namespace(name, {"metadata": {"labels": labels}})

    def delete_namespace(self, name: str, wait: bool = True, timeout: float = 300) -> None:
        try:
            self.core.delete_namespace(name)
        except client.ApiException as e:
            if e.status == 404:
                return
            raise
        if not wait:
            return

        def gone() -> bool:
            try:
                self.core.read_namespace(name)
                return False
            except client.ApiException as e:
                if e.status == 404:
                    return True
                raise

        wait_until(gone, timeout, desc=f"namespace {name} still terminating")

    # -- manifests -----------------------------------------------------------

    def _check_kubectl(self) -> str:
        kubectl = shutil.which("kubectl")
        if not kubectl:
            raise K8osInfraError("kubectl not found on PATH")
        return kubectl

    def _check_helm(self) -> str:
        helm = shutil.which("helm")
        if not helm:
            raise K8osInfraError("helm not found on PATH")
        return helm

    def apply_manifests(
        self, path: Path, namespace: str, variables: dict[str, str] | None = None
    ) -> str:
        """kubectl apply a file or directory into a namespace.

        With `variables`, every ${NAME} occurrence in the YAML text is
        substituted first (used for per-run values like the namespace in
        backup destination paths)."""
        kubectl = self._check_kubectl()
        cmd = [kubectl, "apply", "-n", namespace]
        if self.context:
            cmd += ["--context", self.context]

        stdin: str | None = None
        if variables:
            files = sorted(path.rglob("*.yaml")) if path.is_dir() else [path]
            text = "\n---\n".join(f.read_text() for f in files)
            for key, value in variables.items():
                text = text.replace("${" + key + "}", value)
            stdin = text
            cmd += ["--filename", "-"]
        else:
            cmd += ["--recursive", "--filename", str(path)]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, input=stdin
        )
        if result.returncode != 0:
            raise K8osInfraError(f"kubectl apply failed:\n{result.stderr.strip()}")
        return result.stdout.strip()

    def exec_pod(
        self,
        namespace: str,
        pod: str,
        command: list[str],
        container: str | None = None,
        timeout: int = 120,
    ) -> str:
        """Run a command in a pod via kubectl exec; returns stdout."""
        kubectl = self._check_kubectl()
        cmd = [kubectl, "exec", "-n", namespace, pod]
        if self.context:
            cmd += ["--context", self.context]
        if container:
            cmd += ["-c", container]
        cmd += ["--", *command]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise K8osInfraError(
                f"exec in {pod} failed: {' '.join(command)}\n{result.stderr.strip()}"
            )
        return result.stdout

    def pod_logs(self, namespace: str, pod: str, container: str | None = None) -> str:
        # _preload_content=False + explicit decode: the client otherwise hands
        # back bytes coerced through str(), i.e. "b'...'" with escaped newlines
        resp = self.core.read_namespaced_pod_log(
            pod, namespace, container=container, _preload_content=False
        )
        return resp.data.decode()

    # -- readiness -----------------------------------------------------------

    def wait_workloads_ready(self, namespace: str, timeout: float = 300) -> None:
        """Wait until every Deployment and StatefulSet in the namespace has all
        desired replicas ready. No-op if the namespace has neither."""
        pending: list[str] = []

        def all_ready() -> bool:
            pending.clear()
            for d in self.apps.list_namespaced_deployment(namespace).items:
                desired = d.spec.replicas or 0
                if (d.status.ready_replicas or 0) < desired:
                    pending.append(f"deployment/{d.metadata.name}")
            for s in self.apps.list_namespaced_stateful_set(namespace).items:
                desired = s.spec.replicas or 0
                if (s.status.ready_replicas or 0) < desired:
                    pending.append(f"statefulset/{s.metadata.name}")
            return not pending

        wait_until(all_ready, timeout, desc=lambda: f"not ready: {', '.join(pending)}")


def available_contexts() -> tuple[list[str], str | None]:
    """All kubeconfig context names and the current one."""
    contexts, active = config.list_kube_config_contexts()
    names = [c["name"] for c in contexts]
    return names, active["name"] if active else None
