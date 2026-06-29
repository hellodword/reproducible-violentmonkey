{
  amoDist,
  builtDist,
  diffutils,
  python3,
  release,
  runCommand,
}:

let
  toolchainJson = builtins.toJSON release.toolchain;
in
runCommand "violentmonkey-compare-${release.version}"
  {
    nativeBuildInputs = [
      diffutils
      python3
    ];
    passthru = {
      inherit amoDist builtDist;
    };
  }
  ''
    set -euo pipefail

    built="${builtDist}/share/violentmonkey/dist"
    amo="${amoDist}/share/dist"

    mkdir -p "$out"
    python3 ${../../scripts/make-tree-manifest.py} "$built" > "$out/built-tree.sha256"
    python3 ${../../scripts/make-tree-manifest.py} "$amo" > "$out/amo-tree.sha256"
    cp "${builtDist}/share/violentmonkey/sharp-versions.json" "$out/sharp-versions.json"
    cat > "$out/toolchain.json" <<'EOF'
    ${toolchainJson}
    EOF

    diff -u "$out/amo-tree.sha256" "$out/built-tree.sha256" > "$out/tree-manifest.diff" || manifest_status=$?
    diff -qr "$amo" "$built" > "$out/diff.txt" || tree_status=$?

    python3 - <<'PY' "$out/diffoscope.html" "$out/diff.txt" "$out/tree-manifest.diff"
    import html
    import sys
    from pathlib import Path

    output, tree_diff, manifest_diff = map(Path, sys.argv[1:])
    body = []
    for title, path in [
        ("Recursive diff", tree_diff),
        ("Manifest diff", manifest_diff),
    ]:
        text = path.read_text(encoding="utf-8")
        body.append(f"<h2>{html.escape(title)}</h2><pre>{html.escape(text or 'No differences')}</pre>")
    output.write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>Violentmonkey diffoscope report</title>"
        "<h1>Violentmonkey reproduction report</h1>" + "\n".join(body),
        encoding="utf-8",
    )
    PY

    if [ "''${manifest_status:-0}" != 0 ]; then
      echo "Violentmonkey ${release.version} manifest mismatch" >&2
      cat "$out/tree-manifest.diff" >&2
      echo >&2
      cat "$out/diff.txt" >&2 || true
      exit 1
    fi

    if [ "''${tree_status:-0}" != 0 ]; then
      echo "Violentmonkey ${release.version} tree mismatch" >&2
      cat "$out/diff.txt" >&2
      exit 1
    fi

    cat > "$out/comparison.txt" <<'EOF'
    Violentmonkey ${release.version} reproduced successfully.
    Built dist matches the AMO tree after removing Mozilla META-INF signatures.
    EOF
  ''
