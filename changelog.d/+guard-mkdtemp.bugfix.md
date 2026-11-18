Delegate `guard.mkdtemp` in the Zig build to `zig-common` `fsx.mkdtemp` instead of carrying a local `getrandom` and `mkdirat` loop.
