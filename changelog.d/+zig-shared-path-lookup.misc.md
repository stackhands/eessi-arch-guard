Extract `default_path` and `resolvePath` as public helpers in `zig/guard.zig` so `resolve` and downstream callers share one `PATH` lookup rule.
