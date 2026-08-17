"""
Tests for scaler/scaler.py

Run from node-placeholder-scaler/:
    pytest tests/test_scaler.py
"""

import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from ruamel.yaml import YAML
from scaler.scaler import (
    NodeStatus,
    _process_pool,
    any_placeholder_pod_pending,
    compute_replica_count,
    get_allocatable_resources_by_pool,
    get_node_pool_mapping,
    get_node_status,
    get_replica_counts,
    get_requested_resources_by_pool,
    get_usable_resources,
    make_deployment,
    placeholder_pod_running_on_node,
    update_node_last_above_threshold,
)

yaml = YAML(typ="safe")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEMPLATE = {
    "metadata": {"name": "original-placeholder"},
    "spec": {
        "replicas": 0,
        "template": {
            "spec": {
                "nodeSelector": {},
                "containers": [{"name": "placeholder", "resources": {}}],
            }
        },
    },
}


def _node(name, pool_label=None, label_key="hub.jupyter.org/pool-name"):
    """Return a mock Kubernetes Node object."""
    n = MagicMock()
    n.metadata.name = name
    n.metadata.labels = {label_key: pool_label} if pool_label else {}
    return n


def _alloc_node(name, cpu, memory):
    """Return a mock node with the given allocatable resources."""
    n = MagicMock()
    n.metadata.name = name
    n.status.allocatable = {"cpu": cpu, "memory": memory}
    return n


def _pod(node_name, *container_requests):
    """Return a mock Pod assigned to node_name with the given container requests."""
    p = MagicMock()
    p.spec.node_name = node_name
    containers = []
    for req in container_requests:
        c = MagicMock()
        c.resources.requests = req
        containers.append(c)
    p.spec.containers = containers
    return p


def _running_pod(node_name, phase="Running"):
    """Return a mock Pod with the given node and phase."""
    p = MagicMock()
    p.spec.node_name = node_name
    p.status.phase = phase
    return p


def _event(description, summary="Test Event"):
    """Return a mock calendar event."""
    ev = MagicMock()
    ev.description = description
    ev.summary = summary
    ev.start = MagicMock()
    ev.end = MagicMock()
    ev.computed_duration.days = 0
    return ev


# ---------------------------------------------------------------------------
# make_deployment
# ---------------------------------------------------------------------------


class TestMakeDeployment:
    def test_deployment_name(self):
        d = make_deployment("pool-a", _TEMPLATE, {}, {}, 2)
        assert d["metadata"]["name"] == "pool-a-placeholder"

    def test_replicas(self):
        d = make_deployment("pool-a", _TEMPLATE, {}, {}, 7)
        assert d["spec"]["replicas"] == 7

    def test_zero_replicas(self):
        d = make_deployment("pool-a", _TEMPLATE, {}, {}, 0)
        assert d["spec"]["replicas"] == 0

    def test_node_selector(self):
        selector = {"hub.jupyter.org/pool-name": "pool-a"}
        d = make_deployment("pool-a", _TEMPLATE, selector, {}, 1)
        assert d["spec"]["template"]["spec"]["nodeSelector"] == selector

    def test_resources(self):
        resources = {"requests": {"cpu": "500m", "memory": "1Gi"}}
        d = make_deployment("pool-a", _TEMPLATE, {}, resources, 1)
        assert d["spec"]["template"]["spec"]["containers"][0]["resources"] == resources

    def test_template_not_mutated(self):
        template = deepcopy(_TEMPLATE)
        make_deployment("pool-a", template, {"key": "new-val"}, {"requests": {}}, 3)
        assert template["metadata"]["name"] == "original-placeholder"
        assert template["spec"]["replicas"] == 0
        assert template["spec"]["template"]["spec"]["nodeSelector"] == {}

    def test_pool_name_used_in_deployment_name(self):
        d = make_deployment("gpu-pool", _TEMPLATE, {}, {}, 1)
        assert d["metadata"]["name"] == "gpu-pool-placeholder"


# ---------------------------------------------------------------------------
# get_replica_counts
# ---------------------------------------------------------------------------


class TestGetReplicaCounts:
    def test_single_event(self):
        ev = _event("pool-a: 3\npool-b: 5\n")
        assert get_replica_counts([ev]) == {"pool-a": 3, "pool-b": 5}

    def test_max_across_events(self):
        """When multiple events mention the same pool, take the maximum."""
        ev1 = _event("pool-a: 3\n")
        ev2 = _event("pool-a: 7\n")
        result = get_replica_counts([ev1, ev2])
        assert result["pool-a"] == 7

    def test_max_takes_larger_second(self):
        ev1 = _event("pool-a: 10\n")
        ev2 = _event("pool-a: 2\n")
        result = get_replica_counts([ev1, ev2])
        assert result["pool-a"] == 10

    def test_no_description(self):
        ev = _event(None)
        assert get_replica_counts([ev]) == {}

    def test_empty_event_list(self):
        assert get_replica_counts([]) == {}

    def test_non_integer_value_skipped(self):
        ev = _event("pool-a: not-a-number\n")
        assert get_replica_counts([ev]) == {}

    def test_negative_value_skipped(self):
        ev = _event("pool-a: -1\n")
        assert get_replica_counts([ev]) == {}

    def test_zero_value_preserved(self):
        """An explicit 0 is a real count, not an absent pool."""
        ev = _event("pool-a: 0\n")
        assert get_replica_counts([ev]) == {"pool-a": 0}

    def test_mixed_valid_and_invalid(self):
        ev = _event("pool-a: 5\npool-b: bad\n")
        result = get_replica_counts([ev])
        assert result == {"pool-a": 5}
        assert "pool-b" not in result

    def test_invalid_yaml_skipped(self):
        ev = _event("{{{invalid")
        assert get_replica_counts([ev]) == {}

    def test_description_parses_as_string_skipped(self):
        """A plain-string YAML description (not a dict) is skipped."""
        ev = _event("just a plain string")
        assert get_replica_counts([ev]) == {}

    def test_multiple_pools_across_multiple_events(self):
        ev1 = _event("pool-a: 3\npool-b: 1\n")
        ev2 = _event("pool-b: 5\npool-c: 2\n")
        result = get_replica_counts([ev1, ev2])
        assert result == {"pool-a": 3, "pool-b": 5, "pool-c": 2}


# ---------------------------------------------------------------------------
# get_node_pool_mapping
# ---------------------------------------------------------------------------


class TestGetNodePoolMapping:
    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_basic_mapping(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _node("node-1", "pool-standard"),
            _node("node-2", "pool-gpu"),
        ]
        result = get_node_pool_mapping()
        assert result == {"node-1": "pool-standard", "node-2": "pool-gpu"}

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_node_without_label_gets_unknown_pool(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [_node("node-1")]
        result = get_node_pool_mapping()
        assert result["node-1"] == "unknown-pool"

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_incluster_config_used_when_available(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_api_cls.return_value.list_node.return_value.items = [
            _node("node-1", "pool-a")
        ]
        result = get_node_pool_mapping()
        mock_incluster.assert_called_once()
        mock_kube.assert_not_called()
        assert result == {"node-1": "pool-a"}

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_falls_back_to_kube_config(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = []
        get_node_pool_mapping()
        mock_kube.assert_called_once()

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_custom_label_key(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        label_key = "custom.io/pool"
        mock_api_cls.return_value.list_node.return_value.items = [
            _node("node-1", "my-pool", label_key=label_key)
        ]
        result = get_node_pool_mapping(label_key=label_key)
        assert result["node-1"] == "my-pool"

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_empty_cluster(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = []
        assert get_node_pool_mapping() == {}


# ---------------------------------------------------------------------------
# get_allocatable_resources_by_pool
# ---------------------------------------------------------------------------


class TestGetAllocatableResourcesByPool:
    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_cpu_in_cores(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _alloc_node("node-1", "4", "8Gi")
        ]
        result = get_allocatable_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["cpu_m"] == 4000
        assert result["pool-a"]["node-1"]["mem_mi"] == 8192

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_cpu_in_millicores(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _alloc_node("node-1", "1500m", "2048Mi")
        ]
        result = get_allocatable_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["cpu_m"] == 1500
        assert result["pool-a"]["node-1"]["mem_mi"] == 2048

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_memory_in_kibibytes(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _alloc_node("node-1", "1", "2097152Ki")  # 2048 MiB
        ]
        result = get_allocatable_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["mem_mi"] == 2048

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_nodes_grouped_by_pool(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _alloc_node("node-1", "2", "4Gi"),
            _alloc_node("node-2", "8", "16Gi"),
        ]
        result = get_allocatable_resources_by_pool(
            {"node-1": "pool-cpu", "node-2": "pool-gpu"}
        )
        assert "pool-cpu" in result
        assert "pool-gpu" in result
        assert result["pool-gpu"]["node-2"]["cpu_m"] == 8000

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_node_not_in_mapping_gets_unknown_pool(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _alloc_node("node-99", "2", "4Gi")
        ]
        result = get_allocatable_resources_by_pool({})  # empty mapping
        assert "unknown-pool" in result
        assert "node-99" in result["unknown-pool"]

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_invalid_cpu_defaults_to_zero(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _alloc_node("node-1", "bad-cpu", "1Gi")
        ]
        result = get_allocatable_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["cpu_m"] == 0

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_multiple_nodes_same_pool(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _alloc_node("node-1", "4", "8Gi"),
            _alloc_node("node-2", "4", "8Gi"),
        ]
        result = get_allocatable_resources_by_pool(
            {"node-1": "pool-a", "node-2": "pool-a"}
        )
        assert len(result["pool-a"]) == 2

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_invalid_memory_defaults_to_zero(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_node.return_value.items = [
            _alloc_node("node-1", "2", "bad-memory")
        ]
        result = get_allocatable_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["mem_mi"] == 0


# ---------------------------------------------------------------------------
# get_requested_resources_by_pool
# ---------------------------------------------------------------------------


class TestGetRequestedResourcesByPool:
    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_basic_request(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_pod_for_all_namespaces.return_value.items = [
            _pod("node-1", {"cpu": "500m", "memory": "1Gi"})
        ]
        result = get_requested_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["cpu_m"] == 500
        assert result["pool-a"]["node-1"]["mem_mi"] == 1024

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_multiple_containers_aggregated(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_pod_for_all_namespaces.return_value.items = [
            _pod(
                "node-1",
                {"cpu": "200m", "memory": "512Mi"},
                {"cpu": "300m", "memory": "512Mi"},
            )
        ]
        result = get_requested_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["cpu_m"] == 500
        assert result["pool-a"]["node-1"]["mem_mi"] == 1024

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_multiple_pods_on_same_node_aggregated(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_pod_for_all_namespaces.return_value.items = [
            _pod("node-1", {"cpu": "1", "memory": "1Gi"}),
            _pod("node-1", {"cpu": "1", "memory": "1Gi"}),
        ]
        result = get_requested_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["cpu_m"] == 2000
        assert result["pool-a"]["node-1"]["mem_mi"] == 2048

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_unscheduled_pod_skipped(self, mock_api_cls, mock_incluster, mock_kube):
        """Pods with no node_name (not yet scheduled) should be ignored."""
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_pod_for_all_namespaces.return_value.items = [
            _pod(None, {"cpu": "1", "memory": "1Gi"})
        ]
        result = get_requested_resources_by_pool({"node-1": "pool-a"})
        assert result == {}

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_pods_grouped_by_pool(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_pod_for_all_namespaces.return_value.items = [
            _pod("node-1", {"cpu": "500m", "memory": "512Mi"}),
            _pod("node-2", {"cpu": "2", "memory": "2Gi"}),
        ]
        result = get_requested_resources_by_pool(
            {"node-1": "pool-a", "node-2": "pool-b"}
        )
        assert result["pool-a"]["node-1"]["cpu_m"] == 500
        assert result["pool-b"]["node-2"]["cpu_m"] == 2000

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_zero_requests_default(self, mock_api_cls, mock_incluster, mock_kube):
        """Containers with no resource requests should count as zero."""
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_pod_for_all_namespaces.return_value.items = [
            _pod("node-1", {})  # no requests
        ]
        result = get_requested_resources_by_pool({"node-1": "pool-a"})
        assert result["pool-a"]["node-1"]["cpu_m"] == 0
        assert result["pool-a"]["node-1"]["mem_mi"] == 0

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_no_pods(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_pod_for_all_namespaces.return_value.items = []
        result = get_requested_resources_by_pool({"node-1": "pool-a"})
        assert result == {}


# ---------------------------------------------------------------------------
# get_usable_resources
# ---------------------------------------------------------------------------


class TestGetUsableResources:
    @patch("scaler.scaler.get_requested_resources_by_pool")
    @patch("scaler.scaler.get_allocatable_resources_by_pool")
    @patch("scaler.scaler.get_node_pool_mapping")
    def test_free_resources_computed_correctly(
        self, mock_mapping, mock_alloc, mock_req
    ):
        mock_mapping.return_value = {"node-1": "pool-a"}
        mock_alloc.return_value = {
            "pool-a": {"node-1": {"cpu_m": 4000, "mem_mi": 8192}}
        }
        mock_req.return_value = {"pool-a": {"node-1": {"cpu_m": 1000, "mem_mi": 2048}}}

        result = get_usable_resources()
        node = result["pool-a"]["node-1"]

        assert node["cpu_free_m"] == 3000
        assert node["mem_free_mi"] == 6144
        assert node["cpu_alloc_m"] == 4000
        assert node["mem_alloc_mi"] == 8192
        assert node["cpu_requested_m"] == 1000
        assert node["mem_requested_mi"] == 2048
        assert node["node_pool"] == "pool-a"

    @patch("scaler.scaler.get_requested_resources_by_pool")
    @patch("scaler.scaler.get_allocatable_resources_by_pool")
    @patch("scaler.scaler.get_node_pool_mapping")
    def test_free_ratios_computed_correctly(self, mock_mapping, mock_alloc, mock_req):
        mock_mapping.return_value = {"node-1": "pool-a"}
        mock_alloc.return_value = {
            "pool-a": {"node-1": {"cpu_m": 4000, "mem_mi": 8192}}
        }
        mock_req.return_value = {"pool-a": {"node-1": {"cpu_m": 1000, "mem_mi": 2048}}}

        result = get_usable_resources()
        node = result["pool-a"]["node-1"]

        assert abs(node["cpu_free_ratio"] - 0.75) < 1e-9
        assert abs(node["mem_free_ratio"] - 0.75) < 1e-9

    @patch("scaler.scaler.get_requested_resources_by_pool")
    @patch("scaler.scaler.get_allocatable_resources_by_pool")
    @patch("scaler.scaler.get_node_pool_mapping")
    def test_fully_utilized_node(self, mock_mapping, mock_alloc, mock_req):
        mock_mapping.return_value = {"node-1": "pool-a"}
        mock_alloc.return_value = {
            "pool-a": {"node-1": {"cpu_m": 4000, "mem_mi": 8192}}
        }
        mock_req.return_value = {"pool-a": {"node-1": {"cpu_m": 4000, "mem_mi": 8192}}}

        result = get_usable_resources()
        node = result["pool-a"]["node-1"]

        assert node["cpu_free_m"] == 0
        assert node["mem_free_mi"] == 0
        assert node["cpu_free_ratio"] == 0.0
        assert node["mem_free_ratio"] == 0.0

    @patch("scaler.scaler.get_requested_resources_by_pool")
    @patch("scaler.scaler.get_allocatable_resources_by_pool")
    @patch("scaler.scaler.get_node_pool_mapping")
    def test_multiple_nodes_multiple_pools(self, mock_mapping, mock_alloc, mock_req):
        mock_mapping.return_value = {"node-1": "pool-a", "node-2": "pool-b"}
        mock_alloc.return_value = {
            "pool-a": {"node-1": {"cpu_m": 2000, "mem_mi": 4096}},
            "pool-b": {"node-2": {"cpu_m": 8000, "mem_mi": 16384}},
        }
        mock_req.return_value = {
            "pool-a": {"node-1": {"cpu_m": 500, "mem_mi": 1024}},
            "pool-b": {"node-2": {"cpu_m": 2000, "mem_mi": 4096}},
        }

        result = get_usable_resources()
        assert result["pool-a"]["node-1"]["cpu_free_m"] == 1500
        assert result["pool-b"]["node-2"]["cpu_free_m"] == 6000

    @patch("scaler.scaler.get_requested_resources_by_pool")
    @patch("scaler.scaler.get_allocatable_resources_by_pool")
    @patch("scaler.scaler.get_node_pool_mapping")
    def test_pool_absent_from_requested(self, mock_mapping, mock_alloc, mock_req):
        """Pool exists in alloc but has no pods — requested omits it entirely."""
        mock_mapping.return_value = {"node-1": "pool-a"}
        mock_alloc.return_value = {
            "pool-a": {"node-1": {"cpu_m": 4000, "mem_mi": 8192}}
        }
        mock_req.return_value = {}

        result = get_usable_resources()
        node = result["pool-a"]["node-1"]
        assert node["cpu_free_m"] == 4000
        assert node["mem_free_mi"] == 8192
        assert node["cpu_free_ratio"] == 1.0
        assert node["mem_free_ratio"] == 1.0

    @patch("scaler.scaler.get_requested_resources_by_pool")
    @patch("scaler.scaler.get_allocatable_resources_by_pool")
    @patch("scaler.scaler.get_node_pool_mapping")
    def test_node_absent_from_requested_pool(self, mock_mapping, mock_alloc, mock_req):
        """Pool exists in requested but this specific node has no pods."""
        mock_mapping.return_value = {"node-1": "pool-a", "node-2": "pool-a"}
        mock_alloc.return_value = {
            "pool-a": {
                "node-1": {"cpu_m": 4000, "mem_mi": 8192},
                "node-2": {"cpu_m": 4000, "mem_mi": 8192},
            }
        }
        mock_req.return_value = {"pool-a": {"node-1": {"cpu_m": 1000, "mem_mi": 2048}}}

        result = get_usable_resources()
        assert result["pool-a"]["node-1"]["cpu_free_m"] == 3000
        assert result["pool-a"]["node-2"]["cpu_free_m"] == 4000
        assert result["pool-a"]["node-2"]["cpu_free_ratio"] == 1.0

    @patch("scaler.scaler.get_requested_resources_by_pool")
    @patch("scaler.scaler.get_allocatable_resources_by_pool")
    @patch("scaler.scaler.get_node_pool_mapping")
    def test_zero_allocatable_cpu_no_division_error(
        self, mock_mapping, mock_alloc, mock_req
    ):
        """cpu_m=0 (e.g. parse failure) must not raise ZeroDivisionError."""
        mock_mapping.return_value = {"node-1": "pool-a"}
        mock_alloc.return_value = {"pool-a": {"node-1": {"cpu_m": 0, "mem_mi": 8192}}}
        mock_req.return_value = {"pool-a": {"node-1": {"cpu_m": 0, "mem_mi": 0}}}

        result = get_usable_resources()
        assert result["pool-a"]["node-1"]["cpu_free_ratio"] == 0.0

    @patch("scaler.scaler.get_requested_resources_by_pool")
    @patch("scaler.scaler.get_allocatable_resources_by_pool")
    @patch("scaler.scaler.get_node_pool_mapping")
    def test_zero_allocatable_mem_no_division_error(
        self, mock_mapping, mock_alloc, mock_req
    ):
        """mem_mi=0 (e.g. parse failure) must not raise ZeroDivisionError."""
        mock_mapping.return_value = {"node-1": "pool-a"}
        mock_alloc.return_value = {"pool-a": {"node-1": {"cpu_m": 4000, "mem_mi": 0}}}
        mock_req.return_value = {"pool-a": {"node-1": {"cpu_m": 0, "mem_mi": 0}}}

        result = get_usable_resources()
        assert result["pool-a"]["node-1"]["mem_free_ratio"] == 0.0


# ---------------------------------------------------------------------------
# placeholder_pod_running_on_node
# ---------------------------------------------------------------------------


class TestPlaceholderPodRunningOnNode:
    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_running_pod_on_matching_node(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = [
            _running_pod("node-1", "Running")
        ]
        assert (
            placeholder_pod_running_on_node("node-1", "ns", "app=placeholder") is True
        )

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_running_pod_on_different_node(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = [
            _running_pod("node-2", "Running")
        ]
        assert (
            placeholder_pod_running_on_node("node-1", "ns", "app=placeholder") is False
        )

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_pod_on_node_but_not_running(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = [
            _running_pod("node-1", "Pending")
        ]
        assert (
            placeholder_pod_running_on_node("node-1", "ns", "app=placeholder") is False
        )

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_no_pods(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = []
        assert (
            placeholder_pod_running_on_node("node-1", "ns", "app=placeholder") is False
        )

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_api_error_returns_false(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.side_effect = ApiException()
        assert (
            placeholder_pod_running_on_node("node-1", "ns", "app=placeholder") is False
        )

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_label_selector_passed_to_api(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = []
        placeholder_pod_running_on_node("node-1", "my-ns", "app=test,component=ph")
        mock_api_cls.return_value.list_namespaced_pod.assert_called_once_with(
            namespace="my-ns", label_selector="app=test,component=ph"
        )

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_multiple_pods_one_matches(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = [
            _running_pod("node-2", "Running"),
            _running_pod("node-1", "Running"),
        ]
        assert (
            placeholder_pod_running_on_node("node-1", "ns", "app=placeholder") is True
        )


# ---------------------------------------------------------------------------
# get_node_status
# ---------------------------------------------------------------------------


def _node_with_age(unschedulable, age_seconds):
    node = MagicMock()
    node.spec.unschedulable = unschedulable
    node.metadata.creation_timestamp = datetime.now(timezone.utc) - timedelta(
        seconds=age_seconds
    )
    return node


class TestGetNodeStatus:
    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_unschedulable_true(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.read_node.return_value = _node_with_age(True, 0)
        assert get_node_status("node-1").unschedulable is True

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_unschedulable_false(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.read_node.return_value = _node_with_age(False, 0)
        assert get_node_status("node-1").unschedulable is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_unschedulable_none_treated_as_false(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        """None (field absent) is falsy: cordon sets it to True, not-cordoned is None."""
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.read_node.return_value = _node_with_age(None, 0)
        assert get_node_status("node-1").unschedulable is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_api_error_fails_safe(self, mock_api_cls, mock_incluster, mock_kube):
        """On API error, report schedulable and brand new -- blocks reduction, doesn't allow it."""
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.read_node.side_effect = ApiException()
        status = get_node_status("node-1")
        assert status.unschedulable is False
        assert status.age_seconds == 0.0

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_node_name_passed_to_api(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.read_node.return_value = _node_with_age(False, 0)
        get_node_status("my-special-node")
        mock_api_cls.return_value.read_node.assert_called_once_with(
            name="my-special-node"
        )

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_age_reflects_creation_timestamp(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        """Age comes from the node's real creation time, not first evaluation."""
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.read_node.return_value = _node_with_age(False, 1200)
        age = get_node_status("node-1").age_seconds
        # Allow slack for wall-clock time spent running the test itself.
        assert 1199 <= age <= 1205

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_brand_new_node_age_near_zero(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.read_node.return_value = _node_with_age(False, 0)
        age = get_node_status("node-1").age_seconds
        assert 0 <= age <= 2


# ---------------------------------------------------------------------------
# any_placeholder_pod_pending
# ---------------------------------------------------------------------------

_NODE_SELECTOR = {"hub.jupyter.org/pool-name": "pool-a"}
_OTHER_NODE_SELECTOR = {"hub.jupyter.org/pool-name": "pool-b"}


def _pending_pod(node_selector=None):
    p = MagicMock()
    p.status.phase = "Pending"
    p.spec.node_selector = node_selector or _NODE_SELECTOR
    return p


class TestAnyPlaceholderPodPending:
    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_no_pods_returns_false(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = []
        assert any_placeholder_pod_pending("ns", "app=ph", _NODE_SELECTOR) is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_running_pod_returns_false(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        p = MagicMock()
        p.status.phase = "Running"
        p.spec.node_selector = _NODE_SELECTOR
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = [p]
        assert any_placeholder_pod_pending("ns", "app=ph", _NODE_SELECTOR) is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_pending_pod_matching_pool_returns_true(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = [
            _pending_pod(_NODE_SELECTOR)
        ]
        assert any_placeholder_pod_pending("ns", "app=ph", _NODE_SELECTOR) is True

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_pending_pod_different_pool_returns_false(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        """Pending pod from a different pool must not suppress reduction for this pool."""
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = [
            _pending_pod(_OTHER_NODE_SELECTOR)
        ]
        assert any_placeholder_pod_pending("ns", "app=ph", _NODE_SELECTOR) is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_api_error_returns_false(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.side_effect = ApiException()
        assert any_placeholder_pod_pending("ns", "app=ph", _NODE_SELECTOR) is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_namespace_and_label_selector_passed_to_api(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = []
        any_placeholder_pod_pending(
            "my-ns", "app=ph,component=placeholder", _NODE_SELECTOR
        )
        mock_api_cls.return_value.list_namespaced_pod.assert_called_once_with(
            namespace="my-ns", label_selector="app=ph,component=placeholder"
        )

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_multiple_pods_one_pending_matching_returns_true(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        """Returns True when one of several pods is Pending and matches the pool."""
        mock_incluster.side_effect = ConfigException()
        running = MagicMock()
        running.status.phase = "Running"
        running.spec.node_selector = _NODE_SELECTOR
        mock_api_cls.return_value.list_namespaced_pod.return_value.items = [
            running,
            _pending_pod(_NODE_SELECTOR),
        ]
        assert any_placeholder_pod_pending("ns", "app=ph", _NODE_SELECTOR) is True


# ---------------------------------------------------------------------------
# compute_replica_count
# ---------------------------------------------------------------------------


class TestComputeReplicaCount:
    def test_normal_no_reduction(self):
        assert compute_replica_count(1, 1, None, False) == 1

    def test_normal_with_reduction(self):
        """When a node has spare capacity, reduction brings count to 0."""
        assert compute_replica_count(0, 1, None, False) == 0

    def test_pending_suppresses_reduction(self):
        """Race condition: placeholder evicted but not yet rescheduled."""
        assert (
            compute_replica_count(0, 1, None, False, has_pending_placeholder=True) == 1
        )

    def test_pending_returns_override_not_modified(self):
        """Floor is override_replica_count, not modified_replica, when pending."""
        assert (
            compute_replica_count(-1, 2, None, False, has_pending_placeholder=True) == 2
        )

    def test_pending_preserves_calendar_count_when_override_disabled(self):
        """Calendar count survives the pending window even with override off."""
        assert compute_replica_count(-1, 3, 3, False, has_pending_placeholder=True) == 3

    def test_calendar_override_takes_priority(self):
        assert compute_replica_count(0, 1, 3, True) == 3

    def test_calendar_override_ignores_pending(self):
        """Calendar override is authoritative; pending state doesn't change it."""
        assert compute_replica_count(0, 1, 3, True, has_pending_placeholder=True) == 3

    def test_calendar_count_none_falls_through(self):
        """calendar_replica_count=None means no active calendar event; use normal path."""
        assert compute_replica_count(1, 1, None, True) == 1

    def test_calendar_count_zero_is_honored(self):
        """An event explicitly setting 0 is authoritative, not treated as absent."""
        assert compute_replica_count(1, 1, 0, True) == 0

    def test_calendar_disabled_falls_through(self):
        """calendar_override_enabled=False means ignore calendar count."""
        assert compute_replica_count(0, 1, 3, False) == 0

    def test_modified_replica_floored_at_zero(self):
        """Without pending, result is never negative."""
        assert compute_replica_count(-1, 1, None, False) == 0


# ---------------------------------------------------------------------------
# _process_pool
# ---------------------------------------------------------------------------


def _pool_config(replicas=1):
    return {
        "replicas": replicas,
        "nodeSelector": {"hub.jupyter.org/pool-name": "pool-a"},
        "resources": {"requests": {"memory": "1Mi"}},
    }


def _process_pool_kwargs(**overrides):
    kwargs = dict(
        pool_name="pool-a",
        pool_config=_pool_config(),
        pool_usable_resources={},
        replica_count_overrides={},
        calendar_override_enabled=False,
        placeholder_template={"spec": {"template": {"spec": {"containers": [{}]}}}},
        namespace="node-placeholder",
        label_selector="app=node-placeholder-scaler,component=placeholder",
        strategy="mem",
        cpu_threshold=0.2,
        memory_threshold=0.4,
        new_node_grace_period=600,
        recently_freed_grace_period=600,
        node_last_above_threshold={},
    )
    kwargs.update(overrides)
    return kwargs


def _free_node_status(age_seconds=1000):
    return NodeStatus(unschedulable=False, age_seconds=age_seconds)


class TestProcessPool:
    @patch("scaler.scaler.subprocess.run")
    @patch("scaler.scaler.make_deployment")
    @patch("scaler.scaler.any_placeholder_pod_pending")
    @patch("scaler.scaler.get_node_status")
    @patch("scaler.scaler.placeholder_pod_running_on_node")
    def test_placeholder_node_skips_resource_check_and_is_stamped(
        self, mock_running, mock_status, mock_pending, mock_make_deployment, mock_run
    ):
        mock_running.return_value = True
        mock_status.return_value = _free_node_status()
        mock_pending.return_value = False
        mock_make_deployment.return_value = {}
        node_last_above_threshold = {}
        kwargs = _process_pool_kwargs(
            pool_usable_resources={
                "node-a": {"cpu_free_ratio": 0.9, "mem_free_ratio": 0.9}
            },
            node_last_above_threshold=node_last_above_threshold,
        )
        _process_pool(**kwargs)
        assert "node-a" in node_last_above_threshold

    @patch("scaler.scaler.subprocess.run")
    @patch("scaler.scaler.make_deployment")
    @patch("scaler.scaler.any_placeholder_pod_pending")
    @patch("scaler.scaler.get_node_status")
    @patch("scaler.scaler.placeholder_pod_running_on_node")
    def test_unschedulable_node_skips_resource_check(
        self, mock_running, mock_status, mock_pending, mock_make_deployment, mock_run
    ):
        mock_running.return_value = False
        mock_status.return_value = NodeStatus(unschedulable=True, age_seconds=1000)
        mock_pending.return_value = False
        mock_make_deployment.return_value = {}
        node_last_above_threshold = {}
        kwargs = _process_pool_kwargs(
            pool_config=_pool_config(replicas=1),
            pool_usable_resources={
                "node-a": {"cpu_free_ratio": 0.9, "mem_free_ratio": 0.9}
            },
            node_last_above_threshold=node_last_above_threshold,
        )
        _process_pool(**kwargs)
        # Neither branch that would reduce or stamp the node ran.
        assert "node-a" not in node_last_above_threshold

    @patch("scaler.scaler.subprocess.run")
    @patch("scaler.scaler.make_deployment")
    @patch("scaler.scaler.any_placeholder_pod_pending")
    @patch("scaler.scaler.get_node_status")
    @patch("scaler.scaler.placeholder_pod_running_on_node")
    def test_young_free_node_not_reduced(
        self, mock_running, mock_status, mock_pending, mock_make_deployment, mock_run
    ):
        """A free node younger than the new-node grace period isn't counted."""
        mock_running.return_value = False
        mock_status.return_value = _free_node_status(age_seconds=100)
        mock_pending.return_value = False
        mock_make_deployment.return_value = {}
        node_last_above_threshold = {}
        kwargs = _process_pool_kwargs(
            pool_config=_pool_config(replicas=1),
            pool_usable_resources={
                "node-a": {"cpu_free_ratio": 0.9, "mem_free_ratio": 0.9}
            },
            new_node_grace_period=600,
            node_last_above_threshold=node_last_above_threshold,
        )
        _process_pool(**kwargs)
        assert mock_make_deployment.call_args.args[-1] == 1  # replicas unreduced
        assert "node-a" not in node_last_above_threshold

    @patch("scaler.scaler.subprocess.run")
    @patch("scaler.scaler.make_deployment")
    @patch("scaler.scaler.any_placeholder_pod_pending")
    @patch("scaler.scaler.get_node_status")
    @patch("scaler.scaler.placeholder_pod_running_on_node")
    def test_recently_freed_node_not_reduced(
        self, mock_running, mock_status, mock_pending, mock_make_deployment, mock_run
    ):
        """A free, old-enough node that was above threshold moments ago isn't counted."""
        mock_running.return_value = False
        mock_status.return_value = _free_node_status(age_seconds=1000)
        mock_pending.return_value = False
        mock_make_deployment.return_value = {}
        now = time.perf_counter()
        node_last_above_threshold = {"node-a": now}
        kwargs = _process_pool_kwargs(
            pool_config=_pool_config(replicas=1),
            pool_usable_resources={
                "node-a": {"cpu_free_ratio": 0.9, "mem_free_ratio": 0.9}
            },
            new_node_grace_period=600,
            recently_freed_grace_period=600,
            node_last_above_threshold=node_last_above_threshold,
        )
        _process_pool(**kwargs)
        assert mock_make_deployment.call_args.args[-1] == 1  # replicas unreduced
        # Still flagged as recently freed -- untouched by the reduction branch.
        assert node_last_above_threshold["node-a"] == now

    @patch("scaler.scaler.subprocess.run")
    @patch("scaler.scaler.make_deployment")
    @patch("scaler.scaler.any_placeholder_pod_pending")
    @patch("scaler.scaler.get_node_status")
    @patch("scaler.scaler.placeholder_pod_running_on_node")
    def test_free_node_past_both_grace_periods_is_reduced(
        self, mock_running, mock_status, mock_pending, mock_make_deployment, mock_run
    ):
        mock_running.return_value = False
        mock_status.return_value = _free_node_status(age_seconds=1000)
        mock_pending.return_value = False
        mock_make_deployment.return_value = {}
        kwargs = _process_pool_kwargs(
            pool_config=_pool_config(replicas=1),
            pool_usable_resources={
                "node-a": {"cpu_free_ratio": 0.9, "mem_free_ratio": 0.9}
            },
            new_node_grace_period=600,
            recently_freed_grace_period=600,
            node_last_above_threshold={},
        )
        _process_pool(**kwargs)
        assert mock_make_deployment.call_args.args[-1] == 0  # reduced to 0

    @patch("scaler.scaler.subprocess.run")
    @patch("scaler.scaler.make_deployment")
    @patch("scaler.scaler.any_placeholder_pod_pending")
    @patch("scaler.scaler.get_node_status")
    @patch("scaler.scaler.placeholder_pod_running_on_node")
    def test_not_free_node_is_stamped_and_not_reduced(
        self, mock_running, mock_status, mock_pending, mock_make_deployment, mock_run
    ):
        mock_running.return_value = False
        mock_status.return_value = _free_node_status(age_seconds=1000)
        mock_pending.return_value = False
        mock_make_deployment.return_value = {}
        node_last_above_threshold = {}
        kwargs = _process_pool_kwargs(
            pool_config=_pool_config(replicas=1),
            pool_usable_resources={
                "node-a": {"cpu_free_ratio": 0.1, "mem_free_ratio": 0.1}
            },
            node_last_above_threshold=node_last_above_threshold,
        )
        _process_pool(**kwargs)
        assert "node-a" in node_last_above_threshold
        assert mock_make_deployment.call_args.args[-1] == 1  # not reduced


class TestUpdateNodeLastAboveThreshold:
    def test_new_node_is_recorded(self):
        """First call records the node with the given timestamp."""
        d = {}
        update_node_last_above_threshold("node-a", d, now=1000.0)
        assert d["node-a"] == 1000.0

    def test_existing_node_timestamp_is_updated(self):
        """Subsequent calls overwrite the previous timestamp."""
        d = {"node-a": 500.0}
        update_node_last_above_threshold("node-a", d, now=1000.0)
        assert d["node-a"] == 1000.0

    def test_multiple_nodes_tracked_independently(self):
        """Each node maintains its own last-above-threshold time."""
        d = {}
        update_node_last_above_threshold("node-a", d, now=800.0)
        update_node_last_above_threshold("node-b", d, now=1000.0)
        assert d["node-a"] == 800.0
        assert d["node-b"] == 1000.0

    def test_update_does_not_affect_other_entries(self):
        """Updating one node leaves other entries untouched."""
        d = {"node-a": 500.0}
        update_node_last_above_threshold("node-b", d, now=1000.0)
        assert d["node-a"] == 500.0

    def test_recently_freed_within_grace_period(self):
        """A node last above threshold 100s ago is within a 600s grace period."""
        now = 1000.0
        # stored value is the clock reading when last above threshold, so
        # 100s of elapsed time means last_above = now - 100.
        d = {"node-a": now - 100}
        time_since = now - d["node-a"]
        assert time_since < 600

    def test_recently_freed_exceeds_grace_period(self):
        """A node last above threshold 700s ago exceeds the 600s grace period."""
        now = 1000.0
        # last_above = now - 700 => 700s elapsed, past the 600s threshold.
        d = {"node-a": now - 700}
        time_since = now - d["node-a"]
        assert time_since >= 600

    def test_node_never_above_threshold_not_in_dict(self):
        """A node with no above-threshold history has no entry in the dict."""
        d = {}
        assert "node-a" not in d

    def test_repeated_updates_reflect_latest_time(self):
        """Calling multiple times always stores the most recent timestamp."""
        d = {}
        for t in [100.0, 500.0, 900.0, 1000.0]:
            update_node_last_above_threshold("node-a", d, now=t)
        assert d["node-a"] == 1000.0
