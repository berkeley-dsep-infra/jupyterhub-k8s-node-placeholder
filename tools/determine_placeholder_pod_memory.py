#!/usr/bin/env python3

import re
import subprocess
import sys

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


def kubectl(*args: str) -> str:
    """Run a kubectl command and return its output as a string."""
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <node-name>")
        sys.exit(1)

    node = sys.argv[1]

    # determine node allocatable memory in bytes
    node_mem = kubectl(
        "get", "node", node, "-o", "jsonpath={.status.allocatable.memory}"
    ).strip()
    node_bytes = k8s_mem_to_bytes(node_mem)
    print(f"Node allocatable memory: {node_bytes} bytes")

    # determine total memory requests of all non-placeholder, non-notebook pods on the node
    jsonpath = r"{range .items[*].spec.containers[*]}{.name}{'\t'}{.resources.requests.memory}{'\n'}{end}"
    output = kubectl(
        "get",
        "-A",
        "pod",
        "-l",
        "component!=user-placeholder",
        f"--field-selector=spec.nodeName={node}",
        "-o",
        f"jsonpath={jsonpath}",
    )

    placeholder_pod_mem = 0
    for line in output.splitlines():
        if re.search(r"pause|notebook", line):
            continue
        parts = line.split("\t", 1)
        if len(parts) < 2 or not parts[1].strip():
            continue
        placeholder_pod_mem += k8s_mem_to_bytes(parts[1].strip())

    print(f"Total non-notebook memory used by pods: {placeholder_pod_mem} bytes")

    pod_size = node_bytes - placeholder_pod_mem - UNUSED_MEMORY_BYTES
    print(f"\nRecommended placeholder pod size for values.yaml: {pod_size} bytes")


if __name__ == "__main__":
    main()
