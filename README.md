# Cilium Charm

Cilium is open source software for transparently securing the network connectivity 
between application services deployed using Linux container management platforms. 
At the foundation of Cilium is a new Linux kernel technology called eBPF, which 
enables the dynamic insertion of powerful security visibility and control logic 
within Linux itself.

This charm will deploy Cilium as a background service, and configure CNI for
use with Cilium, on any principal charm that implements the [kubernetes-cni][]
interface.

**Disclaimer:** This is an experimental proof-of-concept charm for using Cilium with
Charmed Kubernetes.

[kubernetes-cni]: https://github.com/juju-solutions/interface-kubernetes-cni

## Cilium releases and upgrades

The default is Cilium **1.20.5**, whose [upstream compatibility matrix](https://github.com/cilium/cilium/blob/v1.20.5/Documentation/network/kubernetes/compatibility.rst)
includes Kubernetes 1.36. Cilium 1.16.19, 1.17.18, 1.18.17 and 1.19.11 are also
bundled for staged upgrades; bundling does not imply Kubernetes 1.36 support for
those older lines. All previously bundled versions are retained.

**Promotion gate:** [PR #27](https://github.com/charmed-kubernetes/charm-cilium/pull/27)
reverted the previous 1.17 default pending dual-stack environment testing. That
testing concern is not resolved by the upstream Kubernetes compatibility matrix.
The integration suite now selects 1.20.5, but does not cover dual-stack clusters
and its existing Hubble flow test remains disabled. Default promotion requires
maintainer-approved IPv4/dual-stack host-environment, Hubble and sequential-upgrade
runtime evidence, plus availability of every image in the configured mirror.

The generated networking configuration remains IPv4-only, with kube-proxy,
cluster-pool IPAM, veth devices and VXLAN tunneling. Existing charm configuration
still controls pod CIDR/mask, VXLAN/Geneve ports, metrics and session affinity.
This update does not add dual-stack pod networking. Cilium 1.20 requires a
[Linux kernel of at least 5.10](https://github.com/cilium/cilium/blob/v1.20.5/Documentation/operations/system_requirements.rst)
on the supported Ubuntu hosts.

### Existing clusters

Do not jump directly from 1.16 to 1.20. The charm checks the installed agent
DaemonSet's tagged version before changing either Cilium or Hubble, blocks
multi-minor transitions and unknown installed versions, and requires a completed
DaemonSet rollout before a version change. API lookup errors are not treated as
fresh installations. A missing DaemonSet (HTTP 404) permits a fresh installation.
An existing cluster with no explicit `release` will be blocked safely after
refresh rather than jumping to the new default.

Following the [upstream upgrade guide](https://github.com/cilium/cilium/blob/v1.20.5/Documentation/operations/upgrade.rst)
and [adjacent-minor requirement](https://github.com/cilium/cilium/blob/v1.20.5/Documentation/operations/upgrade-warning.rst),
use this sequence from the previous default:

```text
1.16.10 -> 1.16.19 -> 1.17.18 -> 1.18.17 -> 1.19.11 -> 1.20.5
```

Before each hop, read that minor's upgrade notes and run upstream's required
preflight checks. Set `juju config cilium release=<next-version>` one step at a
time, and wait for every agent and operator to become ready before continuing.
Check pod/service connectivity, DNS, NetworkPolicy and Hubble after each hop.
Coordinate the Kubernetes upgrade separately: intermediate Cilium versions are
not all supported on Kubernetes 1.36. The charm does not orchestrate these
upgrades automatically.

A held unit names the next release to set (for example `release=1.16.19` on
1.16.10). The charm re-evaluates a held change on every `update-status`, so it
recovers by itself once the cause is gone: an unfinished DaemonSet rollout (the
unit shows `waiting`), a previous `hubble-generate-certs` Job (30-minute TTL in
the 1.16 to 1.18 bundles, immutable pod template) that has not expired yet, or an
unreadable agent image that was restored. No `release` change is needed for
those, and nothing is applied while the change is held. Changing `release`
re-evaluates it immediately.

### Manifest provenance

`upstream/cilium` contains the agent, operator, Envoy, RBAC, Hubble server/peer
and certificate-generation resources. `upstream/hubble` contains **only Hubble
relay**, from the same Cilium chart; it contains neither Hubble UI nor the Hubble
CLI. Relay uses the Cilium version (for example `v1.20.5`). CLI resources are
independent: the Hubble CLI's 1.20 release is `v1.20.4`, not `v1.20.5`, and the
existing `fetch-resources.sh` resource pipeline is unchanged.

`upstream/values.yaml` sets `upgradeCompatibility: "1.16"` for every bundle, which
makes the chart render 1.17+ defaults in their 1.16-compatible form, notably
`enable-policy-secrets-sync` stays off. That is correct for clusters upgraded from
1.16 and is the open question for fresh 1.20.5 installs noted in the pull request.

Regenerate only explicit released chart versions with Helm 3:

```sh
python3 upstream/update.py 1.16.19 1.17.18 1.18.17 1.19.11 1.20.5
```

The renderer uses `https://helm.cilium.io`, Kubernetes 1.36 capabilities and
`upstream/values.yaml`, preserves chart image tags and digests, and refuses to
overwrite existing bundles. It does not choose defaults or mirror images.

## Developers

### Building
To build the Cilium charm:
```
charmcraft pack
```

### Deploying
After you've built the Cilium charm, you can deploy Charmed Kubernetes with Cilium
using the provided overlay:
```
juju deploy charmed-kubernetes --overlay overlay.yaml
```

## Other resources

- See the [Juju SDK documentation](https://juju.is/docs/sdk) for more information about developing and improving charms.
