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
