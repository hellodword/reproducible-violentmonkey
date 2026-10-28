{
  fetcherVersion ? 4,
  nixpkgsPath ? <nixpkgs>,
  pnpmSha256,
  pnpmVersion,
  rev,
  sourceSha256,
  system ? builtins.currentSystem,
  version,
}:

let
  pkgs = import nixpkgsPath { inherit system; };
  fetchPnpmDeps = pkgs.callPackage ./fetch-pnpm-deps.nix { };
  pnpm = pkgs.callPackage ./packages/pnpm.nix {
    version = pnpmVersion;
    hash = pnpmSha256;
  };
  src = pkgs.fetchFromGitHub {
    owner = "violentmonkey";
    repo = "violentmonkey";
    inherit rev;
    hash = sourceSha256;
  };
in
fetchPnpmDeps {
  pname = "violentmonkey-built-dist";
  inherit
    fetcherVersion
    pnpm
    src
    version
    ;
  hash = "";
}
