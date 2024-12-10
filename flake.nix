{
  description = "reproducible-violentmonkey";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs, ... }:
    let
      supportedSystems = [ "x86_64-linux" ];
      forEachSupportedSystem = f: nixpkgs.lib.genAttrs supportedSystems (system: f {
        pkgs = import nixpkgs { inherit system; overlays = [ self.overlays.default ]; };
      });
      info = builtins.fromJSON (builtins.readFile ./info.json);
    in
    {
      overlays.default = final: prev: {
        inherit (self.packages.${prev.system}) violentmonkey-amo violentmonkey-file violentmonkey;
      };

      packages = forEachSupportedSystem
        ({ pkgs }: with pkgs; rec {
          violentmonkey-file = stdenv.mkDerivation rec {
            pname = "violentmonkey-file";
            version = info.version;

            src = pkgs.fetchurl {
              url = info.link;
              sha256 = info.fileHash;
            };

            phases = [ "installPhase" ];
            installPhase = ''
              mkdir -p $out
              cp $src $out
            '';
          };

          violentmonkey-amo = stdenv.mkDerivation rec {
            pname = "violentmonkey-amo";
            version = info.version;

            src = fetchzip rec {
              name = "violentmonkey-${info.version}.xpi.zip";
              # https://github.com/NixOS/nixpkgs/issues/60157#issuecomment-524965720
              url = "${info.link}#${name}";
              hash = info.zipHash;
              stripRoot = false;
            };

            dontPatch = true;
            dontConfigure = true;
            dontBuild = true;
            doCheck = false;
            dontFixup = true;

            installPhase = ''
              runHook preInstall

              # provided by Mozilla
              rm -rf META-INF
              mkdir -p $out/share
              rm -rf $out/share/dist
              cp -r . $out/share/dist

              runHook postInstall
            '';
          };

          violentmonkey =
            let
              pname = "violentmonkey";
              version = info.version;
              node = nodejs_20;
              nodeHeaders = builtins.fetchTarball {
                name = "node-headers-${node.version}";
                url = "https://nodejs.org/download/release/v${node.version}/node-v${node.version}-headers.tar.gz";
                sha256 = info.nodeHeadersHash;
              };
            in
            mkYarnPackage rec  {
              inherit pname version;
              nodejs = node;

              src = fetchFromGitHub {
                owner = "violentmonkey";
                repo = "violentmonkey";
                rev = "v${info.version}";
                hash = info.githubHash;
              };

              packageJSON = "${src}/package.json";
              yarnLock = "${src}/yarn.lock";

              offlineCache = fetchYarnDeps {
                yarnLock = "${src}/yarn.lock";
                hash = info.yarnLockHash;
              };

              pkgConfig = {
                sharp = {
                  nativeBuildInputs = builtins.attrValues {
                    inherit (pkgs.nodePackages) node-gyp;
                    inherit (pkgs) python3 pkg-config;
                  };
                  buildInputs = [ pkgs.vips.dev ];
                  postInstall = "node-gyp --node-dir=${nodeHeaders} rebuild";
                };
              };

              distPhase = "true";
              ignoreScripts = false;

              postBuild = ''
                pushd deps/${pname}

                cat ${./violentmonkey.env} | base64 -d > .env
    
                rm -rf node_modules
                ln -s ../../node_modules node_modules
                yarn --offline run build

                popd
              '';

              installPhase = ''
                runHook preInstall

                rm -rf $out/*
                mkdir -p $out/share
                rm -rf $out/share/dist
                cp -r deps/${pname}/dist $out/share/dist

                runHook postInstall
              '';
            };
        });

      apps = forEachSupportedSystem ({ pkgs }: rec {
        default = diff;
        diff =
          let
            program = pkgs.writeShellApplication {
              name = "exe";
              runtimeInputs = with pkgs; [ diffutils ];
              text = ''
                diff -rq "${pkgs.violentmonkey}/share/dist" "${pkgs.violentmonkey-amo}/share/dist"
              '';
            };
          in
          {
            type = "app";
            program = "${nixpkgs.lib.getExe program}";
          };

        info =
          let
            program = pkgs.writeShellApplication {
              name = "exe";
              runtimeInputs = [ pkgs.nix-prefetch ];
              text = builtins.readFile ./scripts/fetch-info.sh;
              excludeShellChecks = [
                "SC2181"
                "SC2207"
                "SC2005"
                "SC2034"
              ];
            };
          in
          {
            type = "app";
            program = "${nixpkgs.lib.getExe program}";
          };
      });
    };
}
