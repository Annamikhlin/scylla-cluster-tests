# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2022 ScyllaDB

import ast
import json
import logging
from datetime import datetime
from enum import Enum
from json import JSONDecodeError
from tempfile import NamedTemporaryFile
import time

import yaml
from botocore.utils import deep_merge

from sdcm.utils.common import time_period_str_to_seconds
from sdcm.utils.k8s import HelmValues, get_helm_pool_affinity_values

LOGGER = logging.getLogger(__name__)


class ChaosMeshException(Exception):
    pass


class ChaosMeshExperimentException(ChaosMeshException):

    # pylint: disable=too-many-arguments
    def __init__(self, msg: str, experiment: "ChaosMeshExperiment"):
        super().__init__(msg)
        self.message = f"{msg}. Search debug log about {experiment.name}"
        LOGGER.debug(experiment.describe())

    def __str__(self):
        return self.message


class ChaosMeshTimeout(ChaosMeshExperimentException):
    pass


class ChaosMesh:  # pylint: disable=too-few-public-methods
    NAMESPACE = "chaos-mesh"
    VERSION = "2.5.0"
    HELM_SETTINGS = {
        'dashboard': {"create": False},
        'dnsServer': {"create": True}
    }

    def __init__(self, k8s_cluster: "sdcm.cluster_k8s.KubernetesCluster"):
        self._k8s_cluster = k8s_cluster
        self.initialized = False

    def initialize(self) -> None:
        """Installs chaos-mesh on k8s cluster and prepares for future k8s chaos testing."""
        if self._k8s_cluster.kubectl(f"get ns {self.NAMESPACE}", ignore_status=True).ok:
            self.initialized = True
            LOGGER.info("Chaos Mesh is already installed. Skipping installation.")
            return
        LOGGER.info("Installing chaos-mesh on %s k8s cluster...", self._k8s_cluster.k8s_scylla_cluster_name)
        self._k8s_cluster.helm("repo add chaos-mesh https://charts.chaos-mesh.org")
        self._k8s_cluster.helm('repo update')
        self._k8s_cluster.kubectl(f"create namespace {self.NAMESPACE}")
        aux_node_pool_affinity = get_helm_pool_affinity_values(
            self._k8s_cluster.POOL_LABEL_NAME, self._k8s_cluster.AUXILIARY_POOL_NAME)
        scylla_node_pool_affinity = get_helm_pool_affinity_values(
            self._k8s_cluster.POOL_LABEL_NAME, self._k8s_cluster.SCYLLA_POOL_NAME)
        runtime = self._k8s_cluster.kubectl(
            "get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}'").stdout
        runtime_settings = {
            "runtime": "containerd", "socketPath": "/run/containerd/containerd.sock"} if runtime.startswith("containerd") else {}
        chaos_daemon_settings = scylla_node_pool_affinity | runtime_settings
        self._k8s_cluster.helm_install(
            target_chart_name="chaos-mesh",
            source_chart_name="chaos-mesh/chaos-mesh",
            version=self.VERSION,
            use_devel=False,
            namespace=self.NAMESPACE,
            values=HelmValues(self.HELM_SETTINGS | {
                "chaosDaemon": chaos_daemon_settings,
                "controllerManager": aux_node_pool_affinity,
                "dnsServer": aux_node_pool_affinity
            }),
            atomic=True,
            timeout="30m"
        )
        LOGGER.info("chaos-mesh installed successfully on %s k8s cluster.", self._k8s_cluster.k8s_scylla_cluster_name)
        self.initialized = True


class ExperimentStatus(Enum):
    STARTING = 0
    RUNNING = 1
    PAUSED = 2
    FINISHED = 3
    ERROR = 4
    UNKNOWN = 5


class ChaosMeshExperiment:
    """Base class for all chaos-mesh experiments."""
    API_VERSION = "chaos-mesh.org/v1alpha1"
    CHAOS_KIND = ""  # need to override it in child classes

    def __init__(self, pod: "sdcm.cluster_k8s.BasePodContainer", name: str, timeout: int = 0):
        self._k8s_cluster = pod.parent_cluster.k8s_cluster
        self._name = name
        self._namespace = pod.parent_cluster.namespace
        self._experiment = {
            "apiVersion": self.API_VERSION,
            "kind": self.CHAOS_KIND,
            "metadata": {
                "name": self._name,
                "namespace": self._namespace
            },
            "spec": {
                "mode": "one",
                "selector": {
                    "labelSelectors": {
                        "statefulset.kubernetes.io/pod-name": pod.name
                    }
                }
            }
        }
        self._timeout: int = timeout
        self._end_time: int = 0

    @property
    def name(self):
        return self._name

    def start(self):
        """Starts experiment. Does not wait for finish."""
        LOGGER.debug("Starting a pod-failure experiment %s", self._name)
        assert self._k8s_cluster, "K8s cluster hasn't been configured for this experiment."
        with NamedTemporaryFile(suffix=".yaml", mode="w") as experiment_config_file:
            yaml.dump(self._experiment, experiment_config_file)
            experiment_config_file.flush()
            self._k8s_cluster.apply_file(experiment_config_file.name)
        LOGGER.info("'%s' experiment '%s' has started", self.CHAOS_KIND, self._name)
        self._end_time = time.time() + self._timeout

    # pylint: disable=too-many-return-statements
    def get_status(self) -> ExperimentStatus:
        """Gets status of chaos-mesh experiment."""
        result = self._k8s_cluster.kubectl(
            f"get {self.CHAOS_KIND} {self._name} -n {self._namespace} -o jsonpath='{{.status.conditions}}'", verbose=False)
        try:
            condition = {cond["type"]: ast.literal_eval(cond["status"]) for cond in json.loads(result.stdout)}
        except JSONDecodeError:
            # it may happen shortly after startup when command returns empty result
            return ExperimentStatus.UNKNOWN
        if not condition["Selected"] and condition["Paused"]:
            return ExperimentStatus.ERROR
        if not condition["Selected"] and not condition["Paused"] and condition["AllRecovered"] and not condition["AllInjected"]:
            return ExperimentStatus.ERROR
        if not condition["Selected"] and not condition["Paused"]:
            return ExperimentStatus.STARTING
        if condition["Selected"] and not condition["Paused"] and not condition["AllRecovered"] and condition["AllInjected"]:
            return ExperimentStatus.RUNNING
        if condition["AllRecovered"] and condition["Paused"]:
            return ExperimentStatus.PAUSED
        if condition["AllRecovered"] and not condition["Paused"] and condition["Selected"]:
            return ExperimentStatus.FINISHED
        LOGGER.warning("Unknown experiment status: %s", condition)
        return ExperimentStatus.UNKNOWN

    def wait_until_finished(self):
        """Waits given timeout seconds for experiment to finish.

        In case of experiment status being an error or timeout occurred, raises an exception."""
        LOGGER.debug("waiting until '%s' experiment ends...", self._name)
        assert self._end_time, "Experiment was not started. Use 'start()' method before waiting."
        while time.time() < self._end_time:
            status = self.get_status()
            if status == ExperimentStatus.FINISHED:
                LOGGER.debug("'%s' experiment ended.", self._name)
                return
            elif status == ExperimentStatus.ERROR:
                raise ChaosMeshExperimentException(msg="Experiment status error", experiment=self)
            time.sleep(2)
        raise ChaosMeshTimeout(msg="Timeout when waiting for ChaosMesh experiment to complete.", experiment=self)

    def describe(self):
        """Runs kubectl describe experiment resource"""
        return self._k8s_cluster.kubectl(f"describe {self.CHAOS_KIND} {self._name} -n {self._namespace}").stdout


class PodFailureExperiment(ChaosMeshExperiment):
    """
    This experiment works by replacing container image with dummy image.
    Then it waits for specified duration and rolls back image config.
    """
    CHAOS_KIND = "PodChaos"

    def __init__(self, pod: "sdcm.cluster_k8s.BasePodContainer", duration: str):
        """Injects fault into a specified Pod to make the Pod unavailable for a period of time.

            :param sdcm.cluster_k8s.BasePodContainer pod: affected scylla pod
            :param str duration: how long pod will be unavailable. str type in k8s notation. E.g. 10s, 5m
        """
        # timeout based on duration + 10 seconds margin
        timeout = time_period_str_to_seconds(duration) + 10
        super().__init__(
            pod=pod, name=f"pod-failure-{pod.name}-{datetime.now().strftime('%d-%H.%M.%S')}", timeout=timeout)
        deep_merge(self._experiment, {
            "spec": {
                "action": "pod-failure",
                "duration": duration,
            }
        })


class MemoryStressExperiment(ChaosMeshExperiment):
    """
    This experiment uses memStress https://github.com/chaos-mesh/memStress for stressing memory in provided scylla pod.
    """
    CHAOS_KIND = "StressChaos"

    # pylint: disable=too-many-arguments
    def __init__(self, pod: "sdcm.cluster_k8s.BasePodContainer", duration: str, workers: int, size: str, time_to_reach: str | None = None):
        """Stresses memory on scylla pod using https://github.com/chaos-mesh/memStress

            :param sdcm.cluster_k8s.BasePodContainer pod: affected scylla pod
            :param str duration: how long stress will be applied. str type in k8s notation. E.g. 10s, 5m
            :param int workers: Specifies the number of threads that apply memory stress E.g. 4
            :param str size: Specifies the memory size to be occupied or a percentage of the total memory size per worker. E.g.: 256MB / 25%
            :param str time_to_reach: time to reach the size of allocated memory (default "0s")
        """
        # timeout based on duration + 30 seconds margin
        timeout = time_period_str_to_seconds(duration) + 30
        super().__init__(
            pod=pod, name=f"memory-stress-{pod.name}-{datetime.now().strftime('%d-%H.%M.%S')}", timeout=timeout)
        deep_merge(self._experiment, {
            "spec": {
                "duration": duration,
                "containerNames": ["scylla"],
                "stressors": {
                    "memory": {
                        "workers": workers,
                        "size": size,
                    }
                }
            }
        })
        if time_to_reach:
            self._experiment["spec"]["stressors"]["memory"]["options"] = ["-time", time_to_reach]