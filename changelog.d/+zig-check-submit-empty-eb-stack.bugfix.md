`checkSubmit` in `zig/main.zig` falls back from an empty `--eb-stack` value to `config.eb_stack` and then `"eb-stack"` so refusal diagnostics match Python.
