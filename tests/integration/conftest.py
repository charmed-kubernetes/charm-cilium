import logging
import shlex
from pathlib import Path
from typing import Tuple, Union

import pytest
from lightkube import AsyncClient, codecs
from lightkube.config.kubeconfig import KubeConfig
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Pod

log = logging.getLogger(__name__)


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
