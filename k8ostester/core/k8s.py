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
from functools import cached_property
from pathlib import Path

from kubernetes import client, config


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

    def delete_namespace(self, name: str, wait: bool = True, timeout: float = 300) -> None:
        try:
            self.core.delete_namespace(name)
        except client.ApiException as e:
            if e.status == 404:
                return
            raise
        if not wait:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.core.read_namespace(name)
            except client.ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(2)
        raise TimeoutError(f"namespace {name} still terminating after {timeout}s")

    # -- manifests -----------------------------------------------------------

    def apply_manifests(
        self, path: Path, namespace: str, variables: dict[str, str] | None = None
    ) -> str:
        """kubectl apply a file or directory into a namespace.

        With `variables`, every ${NAME} occurrence in the YAML text is
        substituted first (used for per-run values like the namespace in
        backup destination paths)."""
        kubectl = shutil.which("kubectl")
        if not kubectl:
            raise RuntimeError("kubectl not found on PATH")
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
            raise RuntimeError(f"kubectl apply failed:\n{result.stderr.strip()}")
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
        kubectl = shutil.which("kubectl")
        if not kubectl:
            raise RuntimeError("kubectl not found on PATH")
        cmd = [kubectl, "exec", "-n", namespace, pod]
        if self.context:
            cmd += ["--context", self.context]
        if container:
            cmd += ["-c", container]
        cmd += ["--", *command]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(
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
        deadline = time.time() + timeout
        while True:
            pending = []
            for d in self.apps.list_namespaced_deployment(namespace).items:
                desired = d.spec.replicas or 0
                if (d.status.ready_replicas or 0) < desired:
                    pending.append(f"deployment/{d.metadata.name}")
            for s in self.apps.list_namespaced_stateful_set(namespace).items:
                desired = s.spec.replicas or 0
                if (s.status.ready_replicas or 0) < desired:
                    pending.append(f"statefulset/{s.metadata.name}")
            if not pending:
                return
            if time.time() > deadline:
                raise TimeoutError(f"not ready after {timeout}s: {', '.join(pending)}")
            time.sleep(2)


def available_contexts() -> tuple[list[str], str | None]:
    """All kubeconfig context names and the current one."""
    contexts, active = config.list_kube_config_contexts()
    names = [c["name"] for c in contexts]
    return names, active["name"] if active else None
