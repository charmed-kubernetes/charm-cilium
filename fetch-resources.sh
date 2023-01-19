#!/bin/bash
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

set -ex

PROJECTS=("cilium-cli" "hubble")
CILIUM_PROJECT="https://api.github.com/repos/cilium"

# Check the latest release
declare -a assets
for project in "${PROJECTS[@]}"; do
	mapfile -t -O"${#assets[@]}" assets < <(curl -s ${CILIUM_PROJECT}/$project/releases/latest | jq -r '.assets[] | select(.name|match("linux-amd64.tar.gz*")) | .browser_download_url')
done

# Download each resource
printf "%s\n" "${assets[@]}" | xargs -I % curl -sOL %

# Verify sha256 checksum for each resource
# then untar the file
for asset in "${assets[@]}"; do
	if [[ $asset == *.sha256sum ]]; then
		file="${asset##*/}"
		cat "${asset##*/}" | sha256sum -c
		tar -xzf "${file%.*}"
		rm "${file%.*}"*
	fi
done

