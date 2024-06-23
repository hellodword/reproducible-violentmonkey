#! /usr/bin/env bash

set -e

link="$(curl -fsSL "https://addons.mozilla.org/en-US/firefox/addon/violentmonkey" | \
  grep -oP '(?<=")https://addons.mozilla.org/firefox/downloads/file/\d+/[^"\s]+\.xpi(?=")')"

if [ -z "$link" ]; then
  echo "failed to get AMO link"
  exit 1
fi

version="$(echo "$link" | grep -oP "(?<=violentmonkey-)[\d\.]+(?=\.xpi$)")"
if [ -z "$version" ]; then
  echo "failed to get AMO version"
  exit 1
fi

zipHash="$(nix-prefetch "{ fetchzip }: fetchzip { url = \"$link#test.zip\"; stripRoot = false;}")"
if [ -z "$zipHash" ]; then
  echo "failed to get AMO zipHash"
  exit 1
fi

# nodeHeadersHash="$((nix-prefetch '{ node ? nodejs_20 }: builtins.fetchTarball {name = "node-headers-${node.version}"; url = "https://nodejs.org/download/release/v${node.version}/node-v${node.version}-headers.tar.gz"; sha256 = "0000000000000000000000000000000000000000000000000000000000000000";}' 2>&1 || true ) | grep -v '0000000000000000000000000000000000000000000000000000' | grep -oP '(?<=sha256:)[a-z\d]+$')"
# if [ -z "$nodeHeadersHash" ]; then
#   echo "failed to get nodeHeadersHash of nodejs_20"
#   exit 1
# fi

githubHash="$(nix-prefetch fetchFromGitHub --owner violentmonkey --repo violentmonkey --rev "v$version")"
if [ -z "$githubHash" ]; then
  echo "failed to get github hash"
  exit 1
fi

yarnLockHash="$(nix-prefetch "{ fetchYarnDeps, fetchFromGitHub }: let src = fetchFromGitHub { owner = \"violentmonkey\"; repo = \"violentmonkey\"; rev = \"v$version\"; hash = \"$githubHash\"; }; in fetchYarnDeps { yarnLock = \"\${src}/yarn.lock\"; }")"
if [ -z "$yarnLockHash" ]; then
  echo "failed to get yarnLock hash"
  exit 1
fi

jq -n --arg link "$link" --arg version "$version" --arg zipHash "$zipHash" --arg githubHash "$githubHash" --arg yarnLockHash "$yarnLockHash" '
{
  "link": $link,
  "version": $version,
  "zipHash": $zipHash,
  "githubHash": $githubHash,
  "yarnLockHash": $yarnLockHash
}
' | tee info.json

