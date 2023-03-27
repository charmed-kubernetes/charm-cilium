import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Tuple, Union

import juju.utils
import pytest
import yaml
from helpers import get_address
from juju.tag import untag
from lightkube import AsyncClient, codecs
from lightkube.config.kubeconfig import KubeConfig
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Pod
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)


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
        default="",
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
    KubeCtl = Union[str, Tuple[int, str, str]]

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
async def metallb_rbac(kubectl):
    log.info("Applying MetalLB RBAC...")
    await kubectl("apply", "-f", "tests/data/rbac-permissions-operators.yaml")


@pytest.fixture(scope="module")
async def k8s_cloud(request, kubeconfig, module_name, ops_test: OpsTest):
    cloud_name = request.config.getoption("--k8s-cloud") or f"{module_name}-k8s-cloud"
    controller = await ops_test.model.get_controller()
    try:
        current_clouds = await controller.clouds()
        if f"cloud-{cloud_name}" in current_clouds.clouds:
            yield cloud_name
            return
    finally:
        await controller.disconnect()

    with ops_test.model_context("main"):
        log.info(f"Adding cloud '{cloud_name}'...")
        os.environ["KUBECONFIG"] = str(kubeconfig)
        await ops_test.juju(
            "add-k8s",
            cloud_name,
            f"--controller={ops_test.controller_name}",
            "--client",
            check=True,
            fail_msg=f"Failed to add-k8s {cloud_name}",
        )
    yield cloud_name

    with ops_test.model_context("main"):
        log.info(f"Removing cloud '{cloud_name}'...")
        await ops_test.juju(
            "remove-cloud",
            cloud_name,
            "--controller",
            ops_test.controller_name,
            "--client",
            check=True,
        )


@pytest.fixture(scope="module")
async def metal_lb_model(k8s_cloud, ops_test: OpsTest):
    model_alias = "metallb-model"
    log.info("Creating MetalLB model ...")

    model_name = "metallb-system"
    await ops_test.juju(
        "add-model",
        f"--controller={ops_test.controller_name}",
        model_name,
        k8s_cloud,
        "--no-switch",
    )

    model = await ops_test.track_model(
        model_alias,
        model_name=model_name,
        cloud_name=k8s_cloud,
        credential_name=k8s_cloud,
        keep=False,
    )
    model_uuid = model.info.uuid

    yield model, model_alias

    timeout = 5 * 60
    await ops_test.forget_model(model_alias, timeout=timeout, allow_failure=False)

    async def model_removed():
        _, stdout, stderr = await ops_test.juju("models", "--format", "yaml")
        if _ != 0:
            return False
        model_list = yaml.safe_load(stdout)["models"]
        which = [m for m in model_list if m["model-uuid"] == model_uuid]
        return len(which) == 0

    log.info("Removing MetalLB model")
    await juju.utils.block_until_with_coroutine(model_removed, timeout=timeout)
    # Update client's model cache
    await ops_test.juju("models")
    log.info("MetalLB model removed ...")


@pytest.fixture(scope="module")
async def metallb_installed(request, ops_test: OpsTest, metal_lb_model, metallb_rbac):
    ip_range = request.config.getoption("--metallb-iprange")
    log.info(f"Deploying MetalLB with IP range: {ip_range} ...")

    metallb_charms = ["metallb-speaker", "metallb-controller"]
    _, k8s_alias = metal_lb_model
    with ops_test.model_context(k8s_alias) as model:
        await asyncio.gather(
            model.deploy(entity_url="metallb-speaker", trust=True, channel="edge"),
            model.deploy(entity_url="metallb-controller", trust=True, channel="edge"),
        )

        await model.block_until(
            lambda: all(app in model.applications for app in metallb_charms),
            timeout=60,
        )
        await model.wait_for_idle(status="active", timeout=5 * 60)

        metal_controller_app = model.applications["metallb-controller"]
        await metal_controller_app.set_config({"iprange": ip_range})

        await ops_test.model.wait_for_idle(status="active", timeout=5 * 60)

    yield

    with ops_test.model_context(k8s_alias) as m:
        log.info("Removing MetalLB charms...")
        for charm in metallb_charms:
            log.info(f"Removing {charm}...")
            cmd = f"remove-application {charm} --destroy-storage --force"
            rc, stdout, stderr = await ops_test.juju(*shlex.split(cmd))
            log.info(f"{(stdout or stderr)})")
            assert rc == 0
            await m.block_until(lambda: charm not in m.applications, timeout=60 * 10)


@pytest.fixture(scope="module")
async def cos_lb_model(k8s_cloud, ops_test, metallb_installed):
    model_alias = "cos-model"
    log.info("Creating COS model ...")

    model_name = "cos"
    await ops_test.juju(
        "add-model",
        f"--controller={ops_test.controller_name}",
        "--config",
        "controller-service-type=loadbalancer",
        model_name,
        k8s_cloud,
        "--no-switch",
    )

    model = await ops_test.track_model(
        model_alias,
        model_name=model_name,
        cloud_name=k8s_cloud,
        credential_name=k8s_cloud,
        keep=False,
    )
    model_uuid = model.info.uuid

    yield model, model_alias

    timeout = 5 * 60
    await ops_test.forget_model(model_alias, timeout=timeout, allow_failure=False)

    async def model_removed():
        _, stdout, stderr = await ops_test.juju("models", "--format", "yaml")
        if _ != 0:
            return False
        model_list = yaml.safe_load(stdout)["models"]
        which = [m for m in model_list if m["model-uuid"] == model_uuid]
        return len(which) == 0

    log.info("Removing COS model ...")
    await juju.utils.block_until_with_coroutine(model_removed, timeout=timeout)
    # Update client's model cache
    await ops_test.juju("models")
    log.info("COS Model removed ...")


@pytest.fixture(scope="module")
async def cos_lite_installed(ops_test, cos_lb_model):
    log.info("Deploying COS bundle ...")
    cos_charms = ["alertmanager", "catalogue", "grafana", "loki", "prometheus", "traefik"]
    _, k8s_alias = cos_lb_model
    with ops_test.model_context(k8s_alias) as model:
        overlays = [ops_test.Bundle("cos-lite", "edge"), Path("tests/data/offers-overlay.yaml")]

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

    with ops_test.model_context(k8s_alias) as m:
        log.info("Removing COS Lite charms...")
        for charm in cos_charms:
            log.info(f"Removing {charm}...")
            cmd = f"remove-application {charm} --destroy-storage --force"
            rc, stdout, stderr = await ops_test.juju(*shlex.split(cmd))
            log.info(f"{(stdout or stderr)})")
            assert rc == 0
            await m.block_until(lambda: charm not in m.applications, timeout=60 * 10)


@pytest.fixture(scope="module")
async def traefik_ingress(ops_test, cos_lb_model, cos_lite_installed):
    _, k8s_alias = cos_lb_model
    with ops_test.model_context(k8s_alias):
        address = await get_address(ops_test=ops_test, app_name="traefik")
        yield address


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
@pytest.mark.usefixtures("cos_lite_installed")
async def related_grafana(ops_test, cos_lb_model):
    cos_model, k8s_alias = cos_lb_model
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with ops_test.model_context("main") as model:
        log.info("Integrating Grafana and Cilium...")
        await ops_test.model.integrate(
            "cilium:grafana-dashboard", f"{model_owner}/{cos_model_name}.grafana-dashboards"
        )
        with ops_test.model_context(k8s_alias) as model:
            await model.wait_for_idle(status="active")
        await ops_test.model.wait_for_idle(status="active")

    yield

    with ops_test.model_context("main") as model:
        log.info("Removing Grafana SAAS ...")
        await ops_test.model.remove_saas("grafana-dashboards")
        await ops_test.model.wait_for_idle(status="active")
    with ops_test.model_context(k8s_alias) as model:
        log.info("Removing Grafana Offer...")
        await ops_test.model.remove_offer("grafana-dashboard", force=True)
        await ops_test.model.wait_for_idle(status="active")


@pytest.fixture(scope="module")
async def grafana_password(ops_test, related_grafana, cos_lb_model):
    _, k8s_alias = cos_lb_model
    with ops_test.model_context(k8s_alias):
        action = (
            await ops_test.model.applications["grafana"].units[0].run_action("get-admin-password")
        )
        action = await action.wait()
    return action.results["admin-password"]


@pytest.fixture(scope="module")
async def expected_prometheus_metrics():
    metrics_path = Path("tests/data/cilium-metrics.json")
    with open(metrics_path, "r") as file:
        return set(json.load(file)["data"])


@pytest.fixture(scope="module")
@pytest.mark.usefixtures("cos_lite_installed")
async def related_prometheus(ops_test, cos_lb_model):
    cos_model, k8s_alias = cos_lb_model
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with ops_test.model_context("main") as model:
        log.info("Enabling Cilium metrics...")
        cilium_app = ops_test.model.applications["cilium"]
        metrics_config = {"enable-cilium-metrics": "true"}
        await cilium_app.set_config(metrics_config)

        log.info("Integrating Prometheus and Cilium...")
        await ops_test.model.integrate(
            "cilium:send-remote-write",
            f"{model_owner}/{cos_model_name}.prometheus-receive-remote-write",
        )
        await ops_test.model.wait_for_idle(status="active")
        with ops_test.model_context(k8s_alias) as model:
            await model.wait_for_idle(status="active")

    yield

    with ops_test.model_context("main") as model:
        log.info("Removing Cilium metrics...")
        cilium_app = ops_test.model.applications["cilium"]
        metrics_config = {"enable-cilium-metrics": "false"}
        await cilium_app.set_config(metrics_config)

        log.info("Removing Prometheus Remote Write SAAS ...")
        await ops_test.model.remove_saas("prometheus-receive-remote-write")
        await ops_test.model.wait_for_idle(status="active")

    with ops_test.model_context(k8s_alias) as model:
        log.info("Removing Prometheus Offer...")
        await ops_test.model.remove_offer("receive-remote-write", force=True)
        await ops_test.model.wait_for_idle(status="active")
