{
  autoPatchelfHook,
  fetchurl,
  lib,
  makeWrapper,
  pnpm_10,
  stdenv,
  stdenvNoCC,
  version,
  hash,
}:

if lib.versionOlder version "12" then
  pnpm_10.override { inherit version hash; }
else
  stdenvNoCC.mkDerivation {
    pname = "pnpm";
    inherit version;

    src = fetchurl {
      url = "https://registry.npmjs.org/@pnpm/exe.linux-x64/-/exe.linux-x64-${version}.tgz";
      inherit hash;
    };

    nativeBuildInputs = [
      autoPatchelfHook
      makeWrapper
    ];
    buildInputs = [ stdenv.cc.cc.lib ];

    dontConfigure = true;
    dontBuild = true;

    installPhase = ''
      runHook preInstall

      install -Dm755 pnpm "$out/bin/pnpm"
      install -Dm644 LICENSE "$out/share/licenses/pnpm/LICENSE"
      install -Dm644 THIRD-PARTY-NOTICES.md "$out/share/licenses/pnpm/THIRD-PARTY-NOTICES.md"
      makeWrapper "$out/bin/pnpm" "$out/bin/pnpx" --add-flags dlx
      ln -s pnpm "$out/bin/pn"
      ln -s pnpx "$out/bin/pnx"

      runHook postInstall
    '';

    meta = {
      description = "Fast, disk space efficient package manager for JavaScript";
      homepage = "https://pnpm.io/";
      license = lib.licenses.mit;
      platforms = [ "x86_64-linux" ];
      mainProgram = "pnpm";
    };
  }
