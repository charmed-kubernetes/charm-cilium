#!/usr/bin/env python3
"""Render explicit released Cilium charts, splitting the optional Hubble relay.

Requires Helm 3. No image mirroring or default selection is performed. Existing
bundles are never overwritten: remove a newly generated bundle before rerendering.
"""

import argparse
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("versions", nargs="+", help="Exact stable chart versions, without v")
    parser.add_argument("--helm", default="helm", help="Helm 3 executable")
    args = parser.parse_args()
    for version in args.versions:
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            parser.error(f"Not a stable chart version: {version}")
        paths = {name: ROOT / name / "manifests" / version / f"{name}.yaml"
                 for name in ("cilium", "hubble")}
        if any(path.exists() for path in paths.values()):
            parser.error(f"Refusing to overwrite an existing bundle: {version}")
        command = [args.helm, "template", "cilium", "cilium", "--repo",
                   "https://helm.cilium.io", "--version", version, "--namespace",
                   "kube-system", "--kube-version", "1.36.0", "--values",
                   str(ROOT / "values.yaml")]
        rendered = subprocess.check_output(command, text=True)
        bundles = {name: [] for name in paths}
        for document in re.split(r"^---\s*$", rendered, flags=re.MULTILINE):
            if not document.strip():
                continue
            name = "hubble" if "# Source: cilium/templates/hubble-relay/" in document else "cilium"
            bundles[name].append("---\n" + document.lstrip("\n").rstrip() + "\n")
        if not all(bundles.values()):
            raise RuntimeError(f"Chart {version} did not render both Cilium and Hubble relay")
        for name, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(bundles[name]))
            print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
