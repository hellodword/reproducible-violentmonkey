{
  fetchFromGitHub,
  release,
}:

fetchFromGitHub {
  owner = release.upstream.owner;
  repo = release.upstream.repo;
  rev = release.upstream.rev or release.upstream.tag;
  hash = release.upstream.sourceSha256;
}
