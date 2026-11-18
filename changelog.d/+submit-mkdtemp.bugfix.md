Delegate `submitWorkDir` in `zig/main.zig` to `zig-common` `fsx.mkdtemp` instead of carrying a local `getrandom` and `mkdirat` loop.
