{
  description = "Verified Violentmonkey AMO release metadata";

  outputs = { self }:
    let
      entries = builtins.readDir ./releases;
      jsonFiles = builtins.filter (
        name: entries.${name} == "regular" && builtins.match ".*\\.json" name != null
      ) (builtins.attrNames entries);
      readRelease =
        file: builtins.fromJSON (builtins.readFile (./releases + "/${file}"));
      releasesList = map readRelease jsonFiles;
      releases = builtins.listToAttrs (
        map (r: {
          name = r.version;
          value = r;
        }) releasesList
      );
      sortedVersions = builtins.sort (
        a: b: builtins.compareVersions a b < 0
      ) (builtins.attrNames releases);
      latestVersion =
        if sortedVersions == [ ] then
          throw "no verified Violentmonkey releases found in ./releases"
        else
          builtins.elemAt sortedVersions (builtins.length sortedVersions - 1);
      latestRelease = releases.${latestVersion};
      violentmonkeyXpi = {
        inherit (latestRelease) version;
        url = latestRelease.amo.url;
        sha256 = latestRelease.amo.sha256;
      };
    in
    {
      lib = {
        inherit latestVersion releases violentmonkeyXpi;
        violentmonkeyBuildFirefoxXpiAddonArgs = violentmonkeyXpi // {
          pname = "violentmonkey";
          addonId = "{aecec67f-0d10-4fa7-b7c7-609a2db280cf}";
        };
      };
    };
}
