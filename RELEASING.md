# Releasing

A release is a tag `vX.Y.Z` on `main`. The `release` workflow builds the
static Zig binaries for x86_64 and aarch64, runs the Zig tests and the Python
test suite against each binary on its own architecture, builds with
`-Dcommit` set to the tagged commit, checks that the binary's `--version`
names the tag's version and that commit and that the Python package's
`__version__` agrees, builds the wheels for each architecture with cibuildwheel in the manylinux2014
image (one `cp39-abi3`, one `cp313t`, one `cp314t`, each carrying the binary),
and opens a draft release with the binaries, the wheels, the completion files and man
page, `SHA256SUMS` and build provenance attestations. Release builds are reproducible: the same commit
built with Zig 0.17.1 and the same `-Dcommit` gives the same sha256.

`cog bump` does the mechanical part. `cog.toml` runs `tools/release-prepare`
before the bump commit; the script's header lists what it sets and what it
leaves to a person.

## Steps

1. Branch from `main`: `git switch -c chore/release-X.Y.Z origin/main`.
2. Edit, in a commit of its own, the prose the hook leaves alone: the
   `--version` example of `docs/orgmode/reference/cli.org`
   and the "Checked with" line of the tutorial name this release.
3. `cog bump --minor` (or `--patch`, `--major`, `--version X.Y.Z`). It runs
   the hook, commits `chore(version): X.Y.Z` and creates the tag on that
   commit. `cog bump --minor --dry-run` prints the version only.
4. Push the branch, open the pull request, and merge it with a merge commit
   once its checks pass, so the tagged commit is on `main`.
5. Sign the tag and push it:
   `git tag -s -f vX.Y.Z -m "eessi-arch-guard X.Y.Z" 'vX.Y.Z^{}' && git push origin vX.Y.Z`.
6. CI on `main` is green on both architectures, the real-Lmod tests ran (a
   skip counts as a failure), and the root-owned configuration tests ran.
7. Check the draft release: two binaries, six wheels, `SHA256SUMS`, attestations
   (`gh attestation verify FILE --repo stackhands/eessi-arch-guard`), and a
   local build of the tag with the same sha256
   (`zig build -Drelease=true -Dtarget=ARCH-linux-musl -Dcommit=SHA`, where
   SHA is the first 7 characters of the tagged commit).
8. Publish the draft, then update the EasyBuild recipe's version and
   checksums and test-install it.
