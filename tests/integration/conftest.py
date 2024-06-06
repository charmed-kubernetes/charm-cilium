import json
import logging
import shlex
from pathlib import Path
from typing import Tuple, Union

import pytest
from helpers import get_address
from juju.tag import untag
from kubernetes import config as k8s_config
from kubernetes.client import Configuration
from lightkube import AsyncClient, codecs
from lightkube.config.kubeconfig import KubeConfig
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Pod
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)
KubeCtl = Union[str, Tuple[int, str, str]]


def pytest_addoption(parser):
    parser.addoption(
        "--metallb-iprange",
        action="store",
        default="10.246.153.240-10.246.153.242",
        help="IP range to use in MetalLB",
    )
    parser.addoption(
        "--k8s-cloud",
        action="store",
        default=None,
        help="Juju kubernetes cloud to reuse; if not provided, will generate a new cloud",
    )


@pytest.fixture(scope="module")
async def kubeconfig(ops_test):
    kubeconfig_path = ops_test.tmp_path / "kubeconfig"
    retcode, stdout, stderr = await ops_test.run(
        "juju",
        "scp",
        "kubernetes-control-plane/leader:/home/ubuntu/config",
        kubeconfig_path,
    )
    if retcode != 0:
        log.error(f"retcode: {retcode}")
        log.error(f"stdout:\n{stdout.strip()}")
        log.error(f"stderr:\n{stderr.strip()}")
        pytest.fail("Failed to copy kubeconfig from kubernetes-control-plane")
    assert Path(kubeconfig_path).stat().st_size, "kubeconfig file is 0 bytes"
    yield kubeconfig_path


@pytest.fixture(scope="module")
async def kubernetes(kubeconfig):
    config = KubeConfig.from_file(kubeconfig)
    client = AsyncClient(
        config=config.get(context_name="juju-context"),
        trust_env=False,
    )
    yield client


@pytest.fixture(scope="module")
def module_name(request):
    return request.module.__name__.replace("_", "-")


@pytest.fixture(scope="module")
async def cilium_np_resource(kubernetes):
    return create_namespaced_resource(
        "cilium.io", "v2", "CiliumNetworkPolicy", "ciliumnetworkpolicies"
    )


@pytest.fixture(scope="module")
async def hubble_test_resources(kubernetes, cilium_np_resource):
    log.info("Creating Hubble test resources...")
    path = Path("tests/data/hubble-test.yaml")
    pods = []
    for obj in codecs.load_all_yaml(path.read_text()):
        if obj.kind == "Pod":
            pods.append(obj.metadata.name)
        await kubernetes.create(obj)

    for pod in pods:
        await kubernetes.wait(
            Pod,
            pod,
            for_conditions=["Ready"],
            namespace="default",
        )

    yield pods

    log.info("Deleting Hubble test resources...")
    for obj in codecs.load_all_yaml(path.read_text()):
        await kubernetes.delete(type(obj), obj.metadata.name, namespace=obj.metadata.namespace)


@pytest.fixture(scope="module")
def kubectl(ops_test, kubeconfig):
    """Supports running kubectl exec commands."""

    async def f(*args, **kwargs) -> KubeCtl:
        """Actual callable returned by the fixture.

        :returns: if kwargs[check] is True or undefined, stdout is returned
                  if kwargs[check] is False, Tuple[rc, stdout, stderr] is returned
        """
        cmd = ["kubectl", "--kubeconfig", str(kubeconfig)] + list(args)
        check = kwargs["check"] = kwargs.get("check", True)
        rc, stdout, stderr = await ops_test.run(*cmd, **kwargs)
        if not check:
            return rc, stdout, stderr
        return stdout

    return f


@pytest.fixture(scope="module")
def kubectl_exec(kubectl):
    async def f(name: str, namespace: str, cmd: str, **kwds):
        shcmd = f'exec {name} -n {namespace} -- sh -c "{cmd}"'
        return await kubectl(*shlex.split(shcmd), **kwds)

    return f


@pytest.fixture(scope="module")
async def k8s_cloud(request, kubeconfig, ops_test: OpsTest):
    cloud_name = request.config.getoption("--k8s-cloud")
    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(kubeconfig))
    k8s_cloud = await ops_test.add_k8s(
        cloud_name=cloud_name, kubeconfig=config, skip_storage=False
    )
    yield k8s_cloud


@pytest.fixture(scope="module")
async def metallb_model(k8s_cloud, ops_test: OpsTest):
    log.info("Creating MetalLB model ...")

    model_alias = "metallb-model"
    model_name = "metallb-system"
    model = await ops_test.track_model(
        model_alias,
        model_name=model_name,
        cloud_name=k8s_cloud,
        keep=ops_test.ModelKeep.NEVER,
    )

    yield model

    log.info("Removing MetalLB model")
    await ops_test.forget_model(model_alias, timeout=5 * 60, allow_failure=False)
    log.info("MetalLB model removed ...")


@pytest.fixture(scope="module")
async def metallb_installed(request, metallb_model):
    ip_range = request.config.getoption("--metallb-iprange")
    log.info(f"Deploying MetalLB with IP range: {ip_range} ...")

    m = metallb_model
    charm = "metallb"
    await m.deploy(entity_url=charm, trust=True, channel="stable", config={"iprange": ip_range})
    await m.block_until(lambda: charm in m.applications, timeout=60)
    await m.wait_for_idle(status="active", timeout=5 * 60)

    yield

    log.info("Removing MetalLB charm...")
    await m.remove_application(charm, force=True, destroy_storage=True)
    await m.block_until(lambda: charm not in m.applications, timeout=60 * 10)


@pytest.fixture(scope="module")
async def cos_model(k8s_cloud, ops_test, metallb_installed):
    log.info("Creating COS model ...")

    model_alias = "cos-model"
    model_name = "cos"
    model = await ops_test.track_model(
        model_alias,
        model_name=model_name,
        cloud_name=k8s_cloud,
        keep=ops_test.ModelKeep.NEVER,
        config={"controller-service-type": "loadbalancer"},
    )

    yield model

    log.info("Removing COS model ...")
    await ops_test.forget_model(model_alias, timeout=10 * 60, allow_failure=False)
    log.info("COS Model removed ...")


@pytest.fixture(scope="module")
async def cos_lite_installed(ops_test, cos_model):
    log.info("Deploying COS bundle ...")
    cos_charms = [
        "alertmanager",
        "catalogue",
        "grafana",
        "loki",
        "prometheus",
        "traefik",
    ]
    model = cos_model
    overlays = [
        ops_test.Bundle("cos-lite", "edge"),
        Path("tests/data/offers-overlay.yaml"),
    ]
    bundle, *overlays = await ops_test.async_render_bundles(*overlays)
    cmd = f"juju deploy -m {model.name} {bundle} --trust " + " ".join(
        f"--overlay={f}" for f in overlays
    )
    rc, stdout, stderr = await ops_test.run(*shlex.split(cmd))
    assert rc == 0, f"COS Lite failed to deploy: {(stderr or stdout).strip()}"

    await model.block_until(
        lambda: all(app in model.applications for app in cos_charms),
        timeout=60,
    )
    await model.wait_for_idle(status="active", timeout=20 * 60, raise_on_error=False)

    yield

    log.info("Removing COS Lite charms...")
    for charm in cos_charms:
        log.info(f"Removing {charm}...")
        await model.remove_application(charm, force=True, destroy_storage=True)
    await model.block_until(
        lambda: all(app not in model.applications for app in cos_charms),
        timeout=60 * 10,
    )


@pytest.fixture(scope="module")
async def traefik_ingress(cos_model, cos_lite_installed):
    yield await get_address(model=cos_model, app_name="traefik")


@pytest.fixture(scope="module")
async def expected_dashboard_titles():
    grafana_dir = Path("src/grafana_dashboards")
    grafana_files = [p for p in grafana_dir.iterdir() if p.is_file() and p.name.endswith(".json")]
    titles = []
    for path in grafana_files:
        dashboard = json.loads(path.read_text())
        titles.append(dashboard["title"])
    return set(titles)


@pytest.fixture(scope="module")
async def related_grafana(ops_test, cos_model, cos_lite_installed):
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with ops_test.model_context("main") as k8s_model:
        log.info("Integrating Grafana and Cilium...")
        await k8s_model.integrate(
            "cilium:grafana-dashboard",
            f"{model_owner}/{cos_model_name}.grafana-dashboards",
        )
        await cos_model.wait_for_idle(status="active")
        await k8s_model.wait_for_idle(status="active")

    yield

    with ops_test.model_context("main") as k8s_model:
        log.info("Removing Grafana SAAS ...")
        await k8s_model.remove_saas("grafana-dashboards")
        await k8s_model.wait_for_idle(status="active")

        log.info("Removing Grafana Offer...")
        await cos_model.remove_offer(f"{cos_model_name}.grafana-dashboards", force=True)
        await cos_model.wait_for_idle(status="active")


@pytest.fixture(scope="module")
async def grafana_password(ops_test, related_grafana, cos_model):
    action = await cos_model.applications["grafana"].units[0].run_action("get-admin-password")
    action = await action.wait()
    yield action.results["admin-password"]


@pytest.fixture(scope="module")
async def expected_prometheus_metrics():
    metrics_path = Path("tests/data/cilium-metrics.json")
    with open(metrics_path, "r") as file:
        return set(json.load(file)["data"])


@pytest.fixture(scope="module")
async def related_prometheus(ops_test: OpsTest, cos_model, cos_lite_installed):
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with ops_test.model_context("main") as k8s_model:
        log.info("Enabling Cilium metrics...")
        cilium_app = k8s_model.applications["cilium"]
        metrics_config = {"enable-cilium-metrics": "true"}
        await cilium_app.set_config(metrics_config)

        log.info("Integrating Prometheus and Cilium...")
        await k8s_model.integrate(
            "cilium:send-remote-write",
            f"{model_owner}/{cos_model_name}.prometheus-receive-remote-write",
        )
        await k8s_model.wait_for_idle(status="active")
        await cos_model.wait_for_idle(status="active")

    yield

    with ops_test.model_context("main") as k8s_model:
        log.info("Removing Cilium metrics...")
        cilium_app = k8s_model.applications["cilium"]
        metrics_config = {"enable-cilium-metrics": "false"}
        await cilium_app.set_config(metrics_config)

        log.info("Removing Prometheus Remote Write SAAS ...")
        await k8s_model.remove_saas("prometheus-receive-remote-write")
        await k8s_model.wait_for_idle(status="active")

        log.info("Removing Prometheus Offer...")
        await cos_model.remove_offer(
            f"{cos_model_name}.prometheus-receive-remote-write", force=True
        )
        await cos_model.wait_for_idle(status="active")
