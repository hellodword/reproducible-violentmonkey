{
  diffutils,
  fetchPnpmDeps,
  lib,
  nodejs_24,
  pnpm_10,
  pnpmConfigHook,
  release,
  releaseDir,
  stdenv,
  upstreamSrc,
}:

let
  nodejs = nodejs_24;
  pnpm = pnpm_10.override {
    version = release.toolchain.packageManager.nixVersion;
    hash = release.toolchain.packageManager.tarballSha256;
  };
in
stdenv.mkDerivation (finalAttrs: {
  pname = "violentmonkey-built-dist";
  version = release.version;

  src = upstreamSrc;

  pnpmInstallFlags = [
    "--frozen-lockfile"
    "--offline"
    "--package-import-method=copy"
  ];

  pnpmDeps = fetchPnpmDeps {
    inherit (finalAttrs)
      pname
      version
      src
      ;
    inherit pnpm;
    fetcherVersion = release.dependencies.fetcherVersion;
    hash =
      if release.dependencies.pnpmDepsSha256 == null then
        ""
      else
        release.dependencies.pnpmDepsSha256;
  };

  nativeBuildInputs = [
    diffutils
    nodejs
    pnpm
    pnpmConfigHook
  ];

  buildInputs = [ stdenv.cc.cc.lib ];

  env.GITHUB_ACTIONS = "true";
  env.LD_LIBRARY_PATH = lib.makeLibraryPath [ stdenv.cc.cc.lib ];

  __structuredAttrs = true;

  buildPhase = ''
    runHook preBuild

    set -a
    . ${releaseDir}/build.env
    set +a

    node - <<'JS' > sharp-versions.json
    const sharp = require('sharp');
    console.log(JSON.stringify(sharp.versions, null, 2));
    JS

    pnpm build

    mv dist dist.first
    pnpm build
    diff -qr dist dist.first
    rm -rf dist.first

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/share/violentmonkey"
    cp -r dist "$out/share/violentmonkey/dist"
    cp sharp-versions.json "$out/share/violentmonkey/sharp-versions.json"

    runHook postInstall
  '';
})
