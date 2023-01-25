#!/bin/bash
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

set -ex

GH_PROJECTS=("cilium-cli" "hubble")
CILIUM_PROJECT="https://api.github.com/repos/cilium"
CLI_TOOLS=("cilium" "hubble")
ARCH=("amd64" "arm64")

# Check the latest release
declare -a assets
for project in "${GH_PROJECTS[@]}"; do
		for arch in "${ARCH[@]}"; do
			mapfile -t -O"${#assets[@]}" assets < <(curl -s ${CILIUM_PROJECT}/$project/releases/latest | jq -r '.assets[] | select(.name|match("linux-'"$arch"'.tar.gz*")) | .browser_download_url')
		done
done

# Download each resource
printf "%s\n" "${assets[@]}" | xargs -I % curl -sOL %

# Verify sha256 checksum for each resource
for asset in "${assets[@]}"; do
	if [[ $asset == *.sha256sum ]]; then
		file="${asset##*/}"
		cat "${asset##*/}" | sha256sum -c
	fi
done

# Compress binaries into one archive and offer this one as a resource
for cli in "${CLI_TOOLS[@]}"; do
	find . -name "$cli-linux*" | tar -czvf $cli.tar.gz --transform 's:.*/::' -T - 
done

# Cleanup
for asset in "${assets[@]}"; do
	rm "${asset##*/}"
done
