Treat empty `DOCS_SOURCE_REF` and `GITHUB_SHA` values as unset in `docs/tools/zig_api.zig`, so `{ref}` falls back to `main` instead of producing `blob//` source links.
