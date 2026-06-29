{
  nixpkgsPath ? <nixpkgs>,
  root,
  system ? builtins.currentSystem,
  version,
}:

let
  pkgs = import nixpkgsPath { inherit system; };
  rootPath = /. + root;
  releaseDir = rootPath + "/releases/violentmonkey/${version}";
  releaseJsonPath = releaseDir + "/release.json";
  buildEnvPath = releaseDir + "/build.env";
  buildEnvProvenancePath = releaseDir + "/build-env.provenance.json";
  missingMetadataMessage = ''
    release metadata for Violentmonkey ${version} is missing.
    Run: nix run .#update -- ${version}
  '';
  releaseJson =
    if builtins.pathExists releaseJsonPath then
      builtins.fromJSON (builtins.readFile releaseJsonPath)
    else
      throw missingMetadataMessage;
  releaseInfo =
    if releaseJson.version != version then
      throw "release metadata version mismatch: requested ${version}, found ${releaseJson.version}"
    else if !(builtins.pathExists buildEnvPath) then
      throw missingMetadataMessage
    else if !(builtins.pathExists buildEnvProvenancePath) then
      throw missingMetadataMessage
    else
      releaseJson
      // {
        buildEnvText = builtins.readFile buildEnvPath;
        buildEnvProvenance = builtins.fromJSON (builtins.readFile buildEnvProvenancePath);
      };
  upstreamSrc = pkgs.callPackage ./packages/upstream-src.nix {
    release = releaseInfo;
  };
  amoDist = pkgs.stdenv.mkDerivation {
    pname = "violentmonkey-amo";
    inherit version;

    src = pkgs.fetchzip rec {
      name = "violentmonkey-${version}.xpi.zip";
      url = "${releaseInfo.amo.fileUrl}#${name}";
      hash = releaseInfo.amo.fetchzipSha256;
      stripRoot = false;
    };

    dontPatch = true;
    dontConfigure = true;
    dontBuild = true;
    doCheck = false;
    dontFixup = true;

    installPhase = ''
      runHook preInstall

      rm -rf META-INF
      mkdir -p $out/share
      rm -rf $out/share/dist
      cp -r . $out/share/dist

      runHook postInstall
    '';
  };
  builtDist = pkgs.callPackage ./packages/built-dist.nix {
    release = releaseInfo;
    inherit releaseDir upstreamSrc;
  };
in
pkgs.callPackage ./packages/compare.nix {
  release = releaseInfo;
  inherit builtDist amoDist;
}
