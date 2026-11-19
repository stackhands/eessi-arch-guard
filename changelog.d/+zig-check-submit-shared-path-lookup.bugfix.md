`checkSubmit` in `zig/main.zig` uses `guard.resolvePath` for bare `eb-stack` lookups and keeps `guard.lmodInvocable` for slash paths, matching Python `shutil.which` when `PATH` is unset or empty.
