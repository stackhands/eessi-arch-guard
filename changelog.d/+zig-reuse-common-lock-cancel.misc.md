`lockExclusive` in `zig/guard.zig` delegates deadline and cancellation waiting to `zig-common` `state.lockExclusiveCancel` instead of maintaining a local `flock` polling loop.
