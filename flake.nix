{
  description = "reproducible-violentmonkey";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { nixpkgs, ... }:
    let
      supportedSystems = [ "x86_64-linux" ];
      forEachSupportedSystem =
        f:
        nixpkgs.lib.genAttrs supportedSystems (
          system:
          f {
            inherit system;
            pkgs = import nixpkgs { inherit system; };
          }
        );
    in
    {
      apps = forEachSupportedSystem (
        { pkgs, system }:
        let
          repoRoot = ''
            repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
          '';
          updateEnv = ''
            export REPRODUCIBLE_VIOLENTMONKEY_ROOT="$repo_root"
            export REPRODUCIBLE_VIOLENTMONKEY_NIXPKGS="${nixpkgs.outPath}"
          '';
          latest =
            let
              program = pkgs.writeShellApplication {
                name = "violentmonkey-latest";
                runtimeInputs = with pkgs; [
                  git
                  python3
                ];
                text = ''
                  ${repoRoot}
                  ${updateEnv}
                  python3 ${./scripts/update-release.py} --print-latest-version
                '';
              };
            in
            {
              type = "app";
              program = "${nixpkgs.lib.getExe program}";
              meta.description = "Print the latest Violentmonkey version on AMO.";
            };
          update =
            let
              program = pkgs.writeShellApplication {
                name = "violentmonkey-update";
                runtimeInputs = with pkgs; [
                  git
                  nix
                  python3
                ];
                text = ''
                  if [ "$#" -ne 1 ]; then
                    echo "usage: nix run .#update -- <version>" >&2
                    exit 2
                  fi

                  ${repoRoot}
                  ${updateEnv}
                  python3 ${./scripts/update-release.py} "$1"
                '';
              };
            in
            {
              type = "app";
              program = "${nixpkgs.lib.getExe program}";
              meta.description = "Update recorded Violentmonkey release metadata for an explicit version.";
            };
          verify =
            let
              program = pkgs.writeShellApplication {
                name = "violentmonkey-verify";
                runtimeInputs = with pkgs; [
                  git
                  nix
                ];
                text = ''
                  if [ "$#" -ne 1 ]; then
                    echo "usage: nix run .#verify -- <version>" >&2
                    exit 2
                  fi
                  if [ "$1" = latest ]; then
                    echo "verify requires an explicit version; use nix run .#latest to inspect the latest version" >&2
                    exit 2
                  fi

                  ${repoRoot}
                  nix-build --no-out-link ${./.}/nix/verify-release.nix \
                    --argstr root "$repo_root" \
                    --argstr version "$1" \
                    --argstr system "${system}" \
                    --arg nixpkgsPath ${nixpkgs.outPath}
                '';
              };
            in
            {
              type = "app";
              program = "${nixpkgs.lib.getExe program}";
              meta.description = "Verify a recorded Violentmonkey release against the AMO artifact.";
            };
        in
        {
          default = latest;
          inherit latest update verify;
        }
      );
    };
}
