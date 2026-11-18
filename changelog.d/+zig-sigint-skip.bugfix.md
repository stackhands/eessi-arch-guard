Handle `SIGINT` in `zig/main.zig` via the Python `KeyboardInterrupt` skip path (`0`, `78` with `--strict`, or decision JSON with `--json`) instead of terminating with signal `-2`.
