#!/usr/bin/env python3
import argparse
import logging
import subprocess
import tempfile
import time
from copy import deepcopy

from kubernetes import client, config
from ruamel.yaml import YAML

from .calendar_parser import _event_repr, get_calendar, get_events
from .utils import parse_cpu, parse_memory

yaml = YAML(typ="safe")


def get_node_pool_mapping(label_key="hub.jupyter.org/pool-name"):
    """Returns a mapping from node name to node pool label."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    v1 = client.CoreV1Api()
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

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()

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

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()
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

            requested = requested_resources.get(pool).get(node)
            free_cpu = node_info["cpu_m"] - requested["cpu_m"]
            free_mem = node_info["mem_mi"] - requested["mem_mi"]
            usable_resources_result[pool][node] = {
                "cpu_alloc_m": node_info["cpu_m"],
                "cpu_requested_m": requested["cpu_m"],
                "cpu_free_m": free_cpu,
                "cpu_free_ratio": float(free_cpu) / node_info["cpu_m"],
                "mem_alloc_mi": node_info["mem_mi"],
                "mem_requested_mi": requested["mem_mi"],
                "mem_free_mi": free_mem,
                "mem_free_ratio": float(free_mem) / node_info["mem_mi"],
                "node_pool": pool,
            }

    return usable_resources_result


def placeholder_pod_running_on_node(node_name, namespace, label_selector):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()

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
        logging.error(f"Kubernetes API error: {e}")
        return False


def is_unschedulable_node(node_name):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1 = client.CoreV1Api()

    try:
        node = v1.read_node(name=node_name)
        unschedulable = node.spec.unschedulable
        if unschedulable:
            return True
        return False

    except client.exceptions.ApiException as e:
        logging.error(f"Kubernetes API error: {e}")
        return False


def make_deployment(pool_name, template, node_selector, resources, replicas):
    deployment_name = f"{pool_name}-placeholder"
    deployment = deepcopy(template)
    deployment["metadata"]["name"] = deployment_name
    deployment["spec"]["replicas"] = replicas
    deployment["spec"]["template"]["spec"]["nodeSelector"] = node_selector
    deployment["spec"]["template"]["spec"]["containers"][0]["resources"] = resources

    return deployment


log = logging.getLogger(__name__)


def get_replica_counts(events):
    replica_counts = {}
    for ev in events:
        logging.info(f"Found event {_event_repr(ev)}")
        if ev.description:
            # initialize
            pools_replica_config = None
            try:
                pools_replica_config = yaml.load(ev.description)
            except Exception as e:
                logging.error(
                    f"Caught unhandled exception parsing event description:\n{e}"
                )
                logging.error(f"Error in parsing description of {_event_repr(ev)}")
                logging.error(f"{ev.description=}")
                pass
            if pools_replica_config is None:
                logging.error(f"No description in event {_event_repr(ev)}")
                continue
            elif isinstance(pools_replica_config, str):
                logging.error("Event description not parsed as dictionary.")
                logging.error(f"{ev.description=}")
                continue
            for pool_name, count in pools_replica_config.items():
                if not isinstance(count, int):
                    logging.info(f"Count {count} not an integer.")
                    continue
                if pool_name not in replica_counts:
                    replica_counts[pool_name] = count
                else:
                    replica_counts[pool_name] = max(replica_counts[pool_name], count)
        else:
            logging.error(f"Event has no description: {_event_repr(ev)}")
    return replica_counts


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
        "--strategy", choices=["cpu", "mem", "balanced"], default="balanced"
    )

    args = argparser.parse_args()

    namespace = args.namespace
    label_selector = args.placeholder_pod_label_selector
    node_selector_key = args.node_pool_selector_key
    cpu_threshold = args.cpu_threshold
    memory_threshold = args.memory_threshold
    strategy = args.strategy

    while True:
        usable_resources_result = get_usable_resources()
        # Reload all config files on each iteration, so we can change config
        # without needing to bounce the pod
        with open(args.config_file) as f:
            config = yaml.load(f)

        with open(args.placeholder_template_file) as f:
            placeholder_template = yaml.load(f)

        calendar = get_calendar(config["calendarUrl"])

        if calendar:
            events = get_events(calendar)
            logging.info(f"Found {len(events)} events at {config['calendarUrl']}.")

            replica_count_overrides = get_replica_counts(events)
            logging.info(f"Overrides: {replica_count_overrides}")

            # Generate deployment config based on our config
            for pool_name, pool_config in config["nodePools"].items():
                pool_usable_resources = usable_resources_result.get(
                    pool_config["nodeSelector"][node_selector_key], {}
                )
                logging.info(f"Processing the node pool: {pool_name} ... ")
                node_placeholder_deployment_reduction = 0
                for node, resources in pool_usable_resources.items():
                    logging.info(f"Checking node {node} in pool {pool_name} ...")
                    logging.info(
                        f"Node {node} has {resources['cpu_free_ratio']:.2f} CPU free ratio and {resources['mem_free_ratio']:.2f} Memory free ratio."
                    )
                    # Check if a placeholder pod is running on this node
                    placeholder_pod_running = placeholder_pod_running_on_node(
                        node, namespace, label_selector
                    )
                    # Check if the node is unschedulable
                    unschedulable_node = is_unschedulable_node(node)
                    if not placeholder_pod_running and not unschedulable_node:
                        cpu_free_ratio = resources["cpu_free_ratio"]
                        mem_free_ratio = resources["mem_free_ratio"]
                        if (
                            (strategy == "cpu" and cpu_free_ratio > cpu_threshold)
                            or (strategy == "mem" and mem_free_ratio > memory_threshold)
                            or (
                                strategy == "balanced"
                                and (
                                    cpu_free_ratio > cpu_threshold
                                    and mem_free_ratio > memory_threshold
                                )
                            )
                        ):
                            logging.info(
                                f"Node {node} has sufficient resources (Strategy: {strategy}, CPU free ratio: {cpu_free_ratio:.2f}, Memory free ratio: {mem_free_ratio:.2f})."
                            )
                            node_placeholder_deployment_reduction += 1
                    else:
                        logging.info(
                            f"Placeholder pod is running or {node} is unschedulable. Skipping resource check for this node."
                        )

                calendar_replica_count = replica_count_overrides.get(pool_name, 0)
                config_replica_count = pool_config["replicas"]
                calendar_override_enabled = config.get("calendarOverrideEnabled", False)
                if not isinstance(calendar_override_enabled, bool):
                    raise ValueError(
                        f"calendarOverrideEnabled must be a boolean, got {type(calendar_override_enabled).__name__}: {calendar_override_enabled!r}"
                    )
                modified_replica = (
                    replica_count_overrides.get(pool_name, pool_config["replicas"])
                    - node_placeholder_deployment_reduction
                )
                logging.info(
                    f"Calendar replica count for pool {pool_name}: {calendar_replica_count}"
                )

                if calendar_replica_count > 0 and calendar_override_enabled:
                    logging.info(
                        f"Overriding config replica count for pool {pool_name} with calendar replica count {calendar_replica_count} instead of modified replica count: {modified_replica}."
                    )
                    replica_count = max(calendar_replica_count, 0)
                else:
                    logging.info(
                        f"Config replica count for pool {pool_name}: {config_replica_count}"
                    )
                    logging.info(
                        f"Reducing {pool_name} placeholder deployment replicas by {node_placeholder_deployment_reduction} based on node resources."
                    )
                    replica_count = max(modified_replica, 0)
                logging.info(
                    f"Final replica count for pool {pool_name}: {replica_count}"
                )

                deployment = make_deployment(
                    pool_name,
                    placeholder_template,
                    pool_config["nodeSelector"],
                    pool_config["resources"],
                    replica_count,
                )
                logging.info(f"Setting {pool_name} to have {replica_count} replicas")
                with tempfile.NamedTemporaryFile(mode="r+") as f:
                    yaml.dump(deployment, f)
                    f.flush()
                    proc = subprocess.run(
                        ["kubectl", "apply", "-f", f.name],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )

                    logging.info(proc.stdout.strip())
        time.sleep(60)
