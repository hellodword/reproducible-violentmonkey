{
  fetchPnpmDeps,
  lib,
}:

args:
fetchPnpmDeps (
  args
  // lib.optionalAttrs (lib.versionAtLeast args.pnpm.version "12") {
    prePnpmInstall = ''
      export NIX_NPM_REGISTRY="''${NIX_NPM_REGISTRY:-https://registry.npmjs.org/}"
      pnpm config set reporter append-only
    '';
  }
)
