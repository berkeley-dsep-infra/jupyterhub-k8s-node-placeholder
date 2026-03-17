"""
Tests for scaler/scaler.py

Run from node-placeholder-scaler/:
    pytest tests/test_scaler.py
"""

from copy import deepcopy
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from scaler.scaler import (
    get_allocatable_resources_by_pool,
    get_node_pool_mapping,
    get_replica_counts,
    get_requested_resources_by_pool,
    get_usable_resources,
    is_unschedulable_node,
    make_deployment,
    placeholder_pod_running_on_node,
)

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
# is_unschedulable_node
# ---------------------------------------------------------------------------


class TestIsUnschedulableNode:
    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_unschedulable_true(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        node = MagicMock()
        node.spec.unschedulable = True
        mock_api_cls.return_value.read_node.return_value = node
        assert is_unschedulable_node("node-1") is True

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_unschedulable_false(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        node = MagicMock()
        node.spec.unschedulable = False
        mock_api_cls.return_value.read_node.return_value = node
        assert is_unschedulable_node("node-1") is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_unschedulable_none_treated_as_false(
        self, mock_api_cls, mock_incluster, mock_kube
    ):
        """None (field absent) is falsy: cordon sets it to True, not-cordoned is None."""
        mock_incluster.side_effect = ConfigException()
        node = MagicMock()
        node.spec.unschedulable = None
        mock_api_cls.return_value.read_node.return_value = node
        assert is_unschedulable_node("node-1") is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_api_error_returns_false(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        mock_api_cls.return_value.read_node.side_effect = ApiException()
        assert is_unschedulable_node("node-1") is False

    @patch("scaler.scaler.config.load_kube_config")
    @patch("scaler.scaler.config.load_incluster_config")
    @patch("scaler.scaler.client.CoreV1Api")
    def test_node_name_passed_to_api(self, mock_api_cls, mock_incluster, mock_kube):
        mock_incluster.side_effect = ConfigException()
        node = MagicMock()
        node.spec.unschedulable = False
        mock_api_cls.return_value.read_node.return_value = node
        is_unschedulable_node("my-special-node")
        mock_api_cls.return_value.read_node.assert_called_once_with(
            name="my-special-node"
        )
