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
        find "$base" -type f -name "*.json" -exec sed -i "s@${hashes[0]}@${hashes[1]}@" {} \;
    done
}

link="$(curl -fsSL "https://addons.mozilla.org/en-US/firefox/addon/violentmonkey" | \
  grep -oP '(?<=")https://addons.mozilla.org/firefox/downloads/file/\d+/[^"\s]+\.xpi(?=")')"

if [ -z "$link" ]; then
  echo "failed to get AMO link"
  exit 1
fi

echo "link $link"
echo "$(jq --arg link "$link" '.link = $link' info.json)" > info.json

version="$(echo "$link" | grep -oP "(?<=violentmonkey-)[\d\.]+(?=\.xpi$)")"
if [ -z "$version" ]; then
  echo "failed to get AMO version"
  exit 1
fi

echo "version $version"
echo "$(jq --arg version "$version" '.version = $version' info.json)" > info.json

# update_hash violentmonkey violentmonkey/violentmonkey .
