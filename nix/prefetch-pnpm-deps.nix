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
  pnpm = pkgs.pnpm_10.override {
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
pkgs.fetchPnpmDeps {
  pname = "violentmonkey-built-dist";
  inherit
    fetcherVersion
    pnpm
    src
    version
    ;
  hash = "";
}
