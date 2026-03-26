#!/usr/bin/env python3

import re
import sys

from kubernetes import client, config

# leave this much memory available so that the node doesn't become completely
# unschedulable, which can cause issues with cluster autoscaling and other
# components that need to schedule pods on the node
UNUSED_MEMORY_BYTES = 277872640


def k8s_mem_to_bytes(mem: str) -> int:
    """Convert Kubernetes memory string (e.g. "512Mi", "2Gi") to bytes."""
    units = {
        "Ei": 1024**6,
        "Pi": 1024**5,
        "Ti": 1024**4,
        "Gi": 1024**3,
        "Mi": 1024**2,
        "Ki": 1024,
        "E": int(1e18),
        "P": int(1e15),
        "T": int(1e12),
        "G": int(1e9),
        "M": int(1e6),
        "k": int(1e3),
    }
    for suffix, multiplier in units.items():
        if mem.endswith(suffix):
            return int(float(mem[: -len(suffix)]) * multiplier)
    return int(mem)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <node-name>")
        sys.exit(1)

    node = sys.argv[1]

    config.load_kube_config()
    v1 = client.CoreV1Api()

    # determine node allocatable memory in bytes
    node_obj = v1.read_node(node)
    node_mem = node_obj.status.allocatable["memory"]
    node_bytes = k8s_mem_to_bytes(node_mem)
    print(f"Node allocatable memory: {node_bytes} bytes")

    # determine total memory requests of all non-placeholder, non-notebook pods on the node
    pods = v1.list_pod_for_all_namespaces(
        label_selector="component!=user-placeholder",
        field_selector=f"spec.nodeName={node}",
    )

    placeholder_pod_mem = 0
    for pod in pods.items:
        for container in pod.spec.containers:
            if re.search(r"pause|notebook", container.name):
                continue
            mem = (container.resources.requests or {}).get("memory")
            if not mem:
                continue
            placeholder_pod_mem += k8s_mem_to_bytes(mem)

    print(f"Total non-notebook memory used by pods: {placeholder_pod_mem} bytes")

    pod_size = node_bytes - placeholder_pod_mem - UNUSED_MEMORY_BYTES
    print(f"\nRecommended placeholder pod size for values.yaml: {pod_size} bytes")


if __name__ == "__main__":
    main()
