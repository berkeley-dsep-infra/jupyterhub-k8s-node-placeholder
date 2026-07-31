#!/usr/bin/env python3
import argparse
import logging
import subprocess
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone

from kubernetes import client, config
from ruamel.yaml import YAML

from .calendar_parser import _event_repr, get_calendar, get_events
from .utils import parse_cpu, parse_memory

yaml = YAML(typ="safe")
log = logging.getLogger(__name__)


@dataclass
class NodeStatus:
    """A helper data structure to return basic node information."""

    unschedulable: bool
    age_seconds: float


def _get_v1_client() -> client.CoreV1Api:
    """Load kube config and return a CoreV1Api instance."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


def get_node_pool_mapping(label_key="hub.jupyter.org/pool-name"):
    """Returns a mapping from node name to node pool label."""
    v1 = _get_v1_client()
    nodes = v1.list_node().items

    node_to_pool = {}
    for node in nodes:
        name = node.metadata.name
        labels = node.metadata.labels or {}
        pool = labels.get(label_key, "unknown-pool")
        node_to_pool[name] = pool

    return node_to_pool


def get_allocatable_resources_by_pool(node_to_pool_dict):
    """Returns dict: {pool: {node: {'cpu_m': int, 'mem_mi': int}}} with allocatable resources."""
    v1 = _get_v1_client()

    pool_resources = {}
    nodes = v1.list_node().items

    for node in nodes:
        node_name = node.metadata.name
        pool = node_to_pool_dict.get(node_name, "unknown-pool")

        if pool not in pool_resources:
            pool_resources[pool] = {}

        alloc = node.status.allocatable or {}
        cpu_raw = alloc.get("cpu", "0")
        mem_raw = alloc.get("memory", "0")

        try:
            # CPU might be in cores (e.g., "2"), so convert to millicores
            if cpu_raw.endswith("m"):
                cpu_m = int(cpu_raw[:-1])
            else:
                cpu_m = int(float(cpu_raw) * 1000)
        except ValueError:
            cpu_m = 0

        try:
            mem_mi = parse_memory(mem_raw)
        except ValueError:
            mem_mi = 0

        pool_resources[pool][node_name] = {"cpu_m": cpu_m, "mem_mi": mem_mi}

    return pool_resources


def get_requested_resources_by_pool(node_to_pool_dict):
    """Returns dict: {pool: {node: {'cpu_m': int, 'mem_mi': int}}} with requested resources."""
    v1 = _get_v1_client()
    pods = v1.list_pod_for_all_namespaces().items

    pool_resources = {}

    for pod in pods:
        node = pod.spec.node_name
        if not node:
            continue  # Pod not scheduled yet

        pool = node_to_pool_dict.get(node, "unknown-pool")

        if pool not in pool_resources:
            pool_resources[pool] = {}

        if node not in pool_resources[pool]:
            pool_resources[pool][node] = {"cpu_m": 0, "mem_mi": 0}

        for container in pod.spec.containers:
            resources = container.resources.requests or {}
            cpu = resources.get("cpu", "0")
            mem = resources.get("memory", "0")

            try:
                cpu_m = parse_cpu(cpu)
            except ValueError:
                cpu_m = 0

            try:
                mem_mi = parse_memory(mem)
            except ValueError:
                mem_mi = 0

            pool_resources[pool][node]["cpu_m"] += cpu_m
            pool_resources[pool][node]["mem_mi"] += mem_mi

    return pool_resources


def get_usable_resources():
    node_to_pool_dict = get_node_pool_mapping()
    alloc = get_allocatable_resources_by_pool(node_to_pool_dict)
    requested_resources = get_requested_resources_by_pool(node_to_pool_dict)

    usable_resources_result = {}
    for pool, pool_info in alloc.items():
        if pool not in usable_resources_result:
            usable_resources_result[pool] = {}

        for node, node_info in pool_info.items():
            if node not in usable_resources_result[pool]:
                usable_resources_result[pool][node] = {}

            requested = requested_resources.get(pool, {}).get(
                node, {"cpu_m": 0, "mem_mi": 0}
            )
            cpu_alloc = node_info["cpu_m"]
            mem_alloc = node_info["mem_mi"]
            free_cpu = cpu_alloc - requested["cpu_m"]
            free_mem = mem_alloc - requested["mem_mi"]
            usable_resources_result[pool][node] = {
                "cpu_alloc_m": cpu_alloc,
                "cpu_requested_m": requested["cpu_m"],
                "cpu_free_m": free_cpu,
                "cpu_free_ratio": float(free_cpu) / cpu_alloc if cpu_alloc > 0 else 0.0,
                "mem_alloc_mi": mem_alloc,
                "mem_requested_mi": requested["mem_mi"],
                "mem_free_mi": free_mem,
                "mem_free_ratio": float(free_mem) / mem_alloc if mem_alloc > 0 else 0.0,
                "node_pool": pool,
            }

    return usable_resources_result


def placeholder_pod_running_on_node(node_name, namespace, label_selector):
    v1 = _get_v1_client()

    try:
        pods = v1.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector
        ).items

        for pod in pods:
            pod_node = pod.spec.node_name
            pod_phase = pod.status.phase

            if pod_node == node_name and pod_phase == "Running":
                return True

        return False

    except client.exceptions.ApiException as e:
        log.error(f"Kubernetes API error: {e}")
        return False


def any_placeholder_pod_pending(namespace, label_selector, node_selector):
    """Returns True if any placeholder pod for the given pool is Pending.

    Filters by node_selector to avoid suppressing reduction in unrelated pools.
    """
    v1 = _get_v1_client()

    try:
        pods = v1.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector
        ).items

        for pod in pods:
            if (
                pod.status.phase == "Pending"
                and pod.spec.node_selector == node_selector
            ):
                return True

        return False

    except client.exceptions.ApiException as e:
        log.error(f"Kubernetes API error: {e}")
        return False


def compute_replica_count(
    modified_replica,
    override_replica_count,
    calendar_replica_count,
    calendar_override_enabled,
    has_pending_placeholder=False,
):
    """Return the target placeholder replica count for a pool.

    Calendar override takes priority. If a placeholder pod is Pending (evicted
    but not yet rescheduled), suppress reduction and fall back to
    override_replica_count, the same pre-reduction baseline used to compute
    modified_replica (replica_count_overrides.get(pool_name, pool_config["replicas"])),
    so the deployment isn't scaled down during the window before the pod finds
    a new home, and any calendar-provided count is preserved even when calendar
    override isn't enabled.
    """
    if calendar_replica_count is not None and calendar_override_enabled:
        return calendar_replica_count
    elif has_pending_placeholder:
        return max(override_replica_count, 0)
    else:
        return max(modified_replica, 0)


def get_node_status(node_name: str) -> NodeStatus:
    """Get a node's schedulability and age."""
    v1 = _get_v1_client()

    try:
        node = v1.read_node(name=node_name)
        age_seconds = (
            datetime.now(timezone.utc) - node.metadata.creation_timestamp
        ).total_seconds()
        return NodeStatus(
            unschedulable=bool(node.spec.unschedulable), age_seconds=age_seconds
        )

    except client.exceptions.ApiException as e:
        log.error(f"Kubernetes API error: {e}")
        return NodeStatus(unschedulable=False, age_seconds=0.0)


def make_deployment(pool_name, template, node_selector, resources, replicas):
    deployment_name = f"{pool_name}-placeholder"
    deployment = deepcopy(template)
    deployment["metadata"]["name"] = deployment_name
    deployment["spec"]["replicas"] = replicas
    deployment["spec"]["template"]["spec"]["nodeSelector"] = node_selector
    deployment["spec"]["template"]["spec"]["containers"][0]["resources"] = resources

    return deployment


def update_node_last_above_threshold(
    node: str, node_last_above_threshold: dict, now: float
) -> None:
    """Record that a node was seen above the utilization threshold at time `now`.

    Call this whenever a node is hosting a placeholder pod or its utilization
    is above the configured threshold.  The stored timestamp is used to
    enforce the recently-freed grace period: a node that drops below the
    threshold (or loses its placeholder) is not immediately counted for
    replica reduction.  Mutates node_last_above_threshold in place.
    """
    node_last_above_threshold[node] = now


def get_replica_counts(events):
    """Parse calendar events to extract desired replica counts for each pool."""
    replica_counts = {}
    for ev in events:
        log.info(f"Found event {_event_repr(ev)}")
        if ev.description:
            pools_replica_config = None
            try:
                pools_replica_config = yaml.load(ev.description)
            except Exception as e:
                log.error(f"Caught unhandled exception parsing event description:\n{e}")
                log.error(f"Error in parsing description of {_event_repr(ev)}")
                log.error(f"{ev.description=}")
            if pools_replica_config is None:
                log.error(f"No description in event {_event_repr(ev)}")
                continue
            elif isinstance(pools_replica_config, str):
                log.error("Event description not parsed as dictionary.")
                log.error(f"{ev.description=}")
                continue
            for pool_name, count in pools_replica_config.items():
                if not isinstance(count, int):
                    log.info(f"Count {count} for pool {pool_name} not an integer.")
                    continue
                if count < 0:
                    log.error(
                        f"Count {count} for pool {pool_name} is negative; skipping."
                    )
                    continue
                if pool_name not in replica_counts:
                    replica_counts[pool_name] = count
                else:
                    replica_counts[pool_name] = max(replica_counts[pool_name], count)
        else:
            log.error(f"Event has no description: {_event_repr(ev)}")
    return replica_counts


def _process_pool(
    pool_name,
    pool_config,
    pool_usable_resources,
    replica_count_overrides,
    calendar_override_enabled,
    placeholder_template,
    namespace,
    label_selector,
    strategy,
    cpu_threshold,
    memory_threshold,
    new_node_grace_period,
    recently_freed_grace_period,
    node_last_above_threshold,
):
    """Compute and apply the placeholder deployment replica count for one pool."""
    log.info(f"Processing the node pool: {pool_name} ... ")
    node_placeholder_deployment_reduction = 0
    now = time.perf_counter()

    for node, resources in pool_usable_resources.items():
        log.info(f"Checking node {node} in pool {pool_name} ...")
        log.info(
            f"Node {node} has {resources['cpu_free_ratio']:.2f} CPU free ratio and {resources['mem_free_ratio']:.2f} Memory free ratio."
        )
        placeholder_pod_running = placeholder_pod_running_on_node(
            node, namespace, label_selector
        )
        node_status = get_node_status(node)

        if placeholder_pod_running:
            # Node hosts the placeholder — mark it above threshold so
            # the recently-freed grace period applies if the placeholder
            # later moves off (e.g., evicted by a user login).
            update_node_last_above_threshold(node, node_last_above_threshold, now)
            log.info(
                f"Placeholder pod is running on {node}. Skipping resource check for this node."
            )
        elif node_status.unschedulable:
            log.info(
                f"Node {node} is unschedulable. Skipping resource check for this node."
            )
        else:
            node_age_seconds = node_status.age_seconds
            cpu_free_ratio = resources["cpu_free_ratio"]
            mem_free_ratio = resources["mem_free_ratio"]
            is_free = (
                (strategy == "cpu" and cpu_free_ratio > cpu_threshold)
                or (strategy == "mem" and mem_free_ratio > memory_threshold)
                or (
                    strategy == "balanced"
                    and (
                        cpu_free_ratio > cpu_threshold
                        and mem_free_ratio > memory_threshold
                    )
                )
            )
            if not is_free:
                update_node_last_above_threshold(node, node_last_above_threshold, now)
            elif node_age_seconds < new_node_grace_period:
                log.info(
                    f"Node {node} is {node_age_seconds:.0f}s old, within "
                    f"{new_node_grace_period}s new-node grace period. Skipping reduction."
                )
            elif (
                node in node_last_above_threshold
                and (now - node_last_above_threshold[node])
                < recently_freed_grace_period
            ):
                time_since_freed = now - node_last_above_threshold[node]
                log.info(
                    f"Node {node} was above threshold {time_since_freed:.0f}s ago, within "
                    f"{recently_freed_grace_period}s recently-freed grace period. Skipping reduction."
                )
            else:
                log.info(
                    f"Node {node} has sufficient resources (Strategy: {strategy}, CPU free ratio: {cpu_free_ratio:.2f}, Memory free ratio: {mem_free_ratio:.2f})."
                )
                node_placeholder_deployment_reduction += 1

    calendar_replica_count = replica_count_overrides.get(pool_name, None)
    config_replica_count = pool_config["replicas"]
    override_replica_count = replica_count_overrides.get(
        pool_name, pool_config["replicas"]
    )
    modified_replica = override_replica_count - node_placeholder_deployment_reduction
    has_pending_placeholder = any_placeholder_pod_pending(
        namespace, label_selector, pool_config["nodeSelector"]
    )
    log.info(f"Calendar replica count for pool {pool_name}: {calendar_replica_count}")
    log.info(f"Config replica count for pool {pool_name}: {config_replica_count}")
    log.info(
        f"Pending placeholder pod detected for pool {pool_name}: {has_pending_placeholder}"
    )
    if calendar_replica_count is not None and calendar_override_enabled:
        log.info(
            f"Overriding replica count for pool {pool_name} with calendar replica count {calendar_replica_count} instead of modified replica count {modified_replica}."
        )
    elif has_pending_placeholder:
        log.info(
            f"Suppressing reduction for pool {pool_name}: placeholder pod is Pending."
        )
    else:
        log.info(
            f"Reducing {pool_name} placeholder deployment replicas by {node_placeholder_deployment_reduction} based on node resources."
        )

    replica_count = compute_replica_count(
        modified_replica,
        override_replica_count,
        calendar_replica_count,
        calendar_override_enabled,
        has_pending_placeholder,
    )
    log.info(f"Final replica count for pool {pool_name}: {replica_count}")

    deployment = make_deployment(
        pool_name,
        placeholder_template,
        pool_config["nodeSelector"],
        pool_config["resources"],
        replica_count,
    )
    log.info(f"Setting {pool_name} to have {replica_count} replicas")
    with tempfile.NamedTemporaryFile(mode="r+") as f:
        yaml.dump(deployment, f)
        f.flush()
        proc = subprocess.run(
            ["kubectl", "apply", "-f", f.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        log.info(proc.stdout.strip())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    argparser = argparse.ArgumentParser()
    argparser.add_argument("--config-file", default="config.yaml")
    argparser.add_argument(
        "--placeholder-template-file", default="placeholder-template.yaml"
    )
    argparser.add_argument("--namespace", default="node-placeholder")
    argparser.add_argument(
        "--node-pool-selector-key", default="hub.jupyter.org/pool-name"
    )
    argparser.add_argument(
        "--placeholder-pod-label-selector",
        default="app=node-placeholder-scaler,component=placeholder",
    )
    argparser.add_argument("--cpu-threshold", type=float, default=0.2)
    argparser.add_argument("--memory-threshold", type=float, default=0.2)
    argparser.add_argument(
        "--strategy",
        choices=["cpu", "mem", "balanced"],
        default="balanced",
        help=(
            "The resource tracked to trigger a scaling event: cpu, memory, or both (balanced)"
        ),
    )
    argparser.add_argument(
        "--new-node-grace-period",
        type=int,
        default=600,
        help=(
            "Seconds a newly created node is protected from placeholder "
            "reduction to cover node prepull time and not consider it idle."
        ),
    )
    argparser.add_argument(
        "--recently-freed-grace-period",
        type=int,
        default=600,
        help=(
            "Seconds a node is protected from placeholder reduction after "
            "it drops below the utilization threshold or loses its "
            "placeholder pod, to avoid oscillation right after eviction."
        ),
    )

    args = argparser.parse_args()

    namespace = args.namespace
    label_selector = args.placeholder_pod_label_selector
    node_selector_key = args.node_pool_selector_key
    cpu_threshold = args.cpu_threshold
    memory_threshold = args.memory_threshold
    strategy = args.strategy
    new_node_grace_period = args.new_node_grace_period
    recently_freed_grace_period = args.recently_freed_grace_period

    # Maps node name -> perf_counter value when the node was last seen above
    # the utilization threshold (or hosting a placeholder pod).  Used to
    # enforce the recently-freed grace period.
    node_last_above_threshold: dict[str, float] = {}

    while True:
        usable_resources_result = get_usable_resources()
        # Reload all config files on each iteration, so we can change config
        # without needing to bounce the pod
        with open(args.config_file) as f:
            cfg = yaml.load(f)

        with open(args.placeholder_template_file) as f:
            placeholder_template = yaml.load(f)

        if "calendarUrl" in cfg:
            calendar = get_calendar(cfg["calendarUrl"])
        else:
            log.info(
                "No calendarUrl in config; skipping calendar processing and using config replica counts only."
            )
            calendar = None

        if calendar:
            events = get_events(calendar)
            log.info(f"Found {len(events)} events at {cfg['calendarUrl']}.")
            replica_count_overrides = get_replica_counts(events)
            log.info(f"Overrides: {replica_count_overrides}")
        else:
            log.info("No calendar available; using config replica counts only.")
            replica_count_overrides = {}

        calendar_override_enabled = cfg.get("calendarOverrideEnabled", False)
        if not isinstance(calendar_override_enabled, bool):
            raise ValueError(
                f"calendarOverrideEnabled must be a boolean, got {type(calendar_override_enabled).__name__}: {calendar_override_enabled!r}"
            )

        for pool_name, pool_config in cfg["nodePools"].items():
            pool_usable_resources = usable_resources_result.get(
                pool_config["nodeSelector"][node_selector_key], {}
            )
            _process_pool(
                pool_name=pool_name,
                pool_config=pool_config,
                pool_usable_resources=pool_usable_resources,
                replica_count_overrides=replica_count_overrides,
                calendar_override_enabled=calendar_override_enabled,
                placeholder_template=placeholder_template,
                namespace=namespace,
                label_selector=label_selector,
                strategy=strategy,
                cpu_threshold=cpu_threshold,
                memory_threshold=memory_threshold,
                new_node_grace_period=new_node_grace_period,
                recently_freed_grace_period=recently_freed_grace_period,
                node_last_above_threshold=node_last_above_threshold,
            )

        # Evict tracking entries for nodes no longer present in the cluster.
        all_seen_nodes = {
            node
            for pool_nodes in usable_resources_result.values()
            for node in pool_nodes
        }
        node_last_above_threshold = {
            n: t for n, t in node_last_above_threshold.items() if n in all_seen_nodes
        }
        time.sleep(60)
