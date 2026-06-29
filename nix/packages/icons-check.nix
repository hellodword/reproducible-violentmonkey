{
  amoDist,
  builtDist,
  diffutils,
  python3,
  release,
  runCommand,
}:

runCommand "violentmonkey-icons-check-${release.version}"
{
  nativeBuildInputs = [
    diffutils
    python3
  ];
}
  ''
    set -eu

    built_icons="${builtDist}/share/violentmonkey/dist/public/images"
    amo_icons="${amoDist}/share/dist/public/images"
    sharp_versions="${builtDist}/share/violentmonkey/sharp-versions.json"

    test -d "$built_icons"
    test -d "$amo_icons"
    test -f "$sharp_versions"

    python3 - "$sharp_versions" <<'PY'
    import json
    import sys

    with open(sys.argv[1], encoding="utf-8") as handle:
        versions = json.load(handle)

    missing = [name for name in ("sharp", "vips", "png") if not versions.get(name)]
    if missing:
        raise SystemExit(f"missing sharp smoke versions: {', '.join(missing)}")

    print(f"sharp {versions['sharp']} / vips {versions['vips']} / png {versions['png']}")
    PY

    diff -qr "$built_icons" "$amo_icons"

    mkdir -p "$out"
    cp "$sharp_versions" "$out/sharp-versions.json"
    (
      cd "$built_icons"
      find . -type f -name '*.png' -print0 \
        | sort -z \
        | xargs -0 sha256sum
    ) > "$out/icons.sha256"
  ''
