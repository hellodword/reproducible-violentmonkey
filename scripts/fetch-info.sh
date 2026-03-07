#! /usr/bin/env bash

set +e

update_hash() {
    pkg="$1"
    repo="$2"
    base="$3"

    while true
    do
        result="$(nix build .#"$pkg" 2>&1)"
        if [ $? -eq 0 ]; then
            break
        fi

        hashes=( $(echo "$result" | grep -A2 'error: hash mismatch' | grep -oP 'sha256-.{44}') )
        if [ ${#hashes[@]} -ne 2 ]; then
            echo "$result"
            exit 1
        fi
        echo "$pkg" upgrading "${hashes[0]}" to "${hashes[1]}"
        sed -i "s@${hashes[0]}@${hashes[1]}@" info.json
    done
}


body="$(curl -fsSL 'https://addons.mozilla.org/api/v5/addons/addon/violentmonkey/versions/?page=1&lang=en-US')"
link="$(echo "$body" | jq -r '.results[0].file.url')"
if [ -z "$link" ]; then
  echo "failed to get AMO link"
  exit 1
fi

echo "link $link"
echo "$(jq --arg link "$link" '.link = $link' info.json)" > info.json

version="$(echo "$body" | jq -r '.results[0].version')"
if [ -z "$version" ]; then
  echo "failed to get AMO version"
  exit 1
fi

echo "version $version"
echo "$(jq --arg version "$version" '.version = $version' info.json)" > info.json

update_hash violentmonkey-file violentmonkey/violentmonkey .
# update_hash violentmonkey-amo violentmonkey/violentmonkey .
# update_hash violentmonkey violentmonkey/violentmonkey .
