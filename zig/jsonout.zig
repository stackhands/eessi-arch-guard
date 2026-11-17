//! One-line JSON for `--json`; schema_version 1, stable keys.
const std = @import("std");
const common = @import("zig_common");
const cli = common.cli;
const guard = @import("guard.zig");
const envmap = @import("envmap.zig");

const json = common.json;
const Allocator = std.mem.Allocator;
const Map = envmap.Map;
const Outcome = guard.Outcome;
const Py = json.Py;

pub const schema_version: u32 = 1;

fn oom() noreturn {
    cli.die("eessi-arch-guard", "out of memory", .{});
}

fn reloadNeeded(kind: guard.Kind) bool {
    return kind == .reloaded or kind == .failed;
}

fn exitSemantic(exit_code: u8) []const u8 {
    if (exit_code == 78) return "strict-failure";
    if (exit_code == 1) return "restore-failed";
    return "ok";
}

fn skipCause(detail: ?[]const u8) []const u8 {
    const s = detail orelse "";
    if (std.mem.find(u8, s, "EESSI_ARCH_GUARD=0") != null) return "opt-out";
    if (std.mem.find(u8, s, "not loaded") != null) return "not-loaded";
    if (std.mem.find(u8, s, "OVERRIDE") != null) return "pinned";
    if (std.mem.find(u8, s, "ignored") != null) return "no-software-dir-ignored";
    return "skipped";
}

fn splitArrow(s: []const u8) ?struct { lhs: []const u8, rhs: []const u8 } {
    const at = std.mem.find(u8, s, "->") orelse return null;
    return .{ .lhs = s[0..at], .rhs = s[at + 2 ..] };
}

fn pyOpt(value: ?[]const u8) Py {
    if (value) |v| return .{ .str = v };
    return .null;
}

/// The fields `--json` prints as one object.
pub fn decisionView(
    a: Allocator,
    outcome: Outcome,
    env: Map,
    cached: bool,
    strict: bool,
    shell: bool,
    exit_code: u8,
) Allocator.Error!Py {
    var was = env.get("EESSI_SOFTWARE_SUBDIR");
    var now: ?[]const u8 = null;
    var cause: ?[]const u8 = null;
    switch (outcome.kind) {
        .reloaded => {
            const marker = (outcome.changes.get("EESSI_ARCH_GUARD_RELOADED") orelse null) orelse "";
            if (splitArrow(marker)) |pair| {
                was = pair.lhs;
                now = pair.rhs;
            } else {
                now = outcome.changes.get("EESSI_SOFTWARE_SUBDIR") orelse null;
            }
        },
        .failed => {
            const marker = (outcome.changes.get("EESSI_ARCH_GUARD_FAILED") orelse null) orelse "";
            if (splitArrow(marker)) |pair| {
                was = pair.lhs;
                cause = pair.rhs;
            } else {
                cause = "failed";
            }
        },
        .skip => cause = skipCause(outcome.detail),
        .noop => now = was,
    }
    var fields: std.ArrayList(Py.Entry) = .empty;
    try fields.append(a, .{ .key = "cached", .value = .{ .bool = cached } });
    try fields.append(a, .{ .key = "cause", .value = pyOpt(cause) });
    try fields.append(a, .{ .key = "decision", .value = .{ .str = @tagName(outcome.kind) } });
    try fields.append(a, .{ .key = "detail", .value = pyOpt(outcome.detail) });
    try fields.append(a, .{ .key = "exit", .value = .{ .int = exit_code } });
    try fields.append(a, .{ .key = "exit_semantic", .value = .{ .str = exitSemantic(exit_code) } });
    try fields.append(a, .{ .key = "kind", .value = .{ .str = "decision" } });
    try fields.append(a, .{ .key = "message", .value = pyOpt(outcome.message) });
    try fields.append(a, .{ .key = "now", .value = pyOpt(now) });
    try fields.append(a, .{ .key = "reload_needed", .value = .{ .bool = reloadNeeded(outcome.kind) } });
    try fields.append(a, .{ .key = "schema_version", .value = .{ .int = schema_version } });
    try fields.append(a, .{ .key = "shell", .value = .{ .bool = shell } });
    try fields.append(a, .{ .key = "strict", .value = .{ .bool = strict } });
    try fields.append(a, .{ .key = "was", .value = pyOpt(was) });
    return .{ .dict = fields.items };
}

/// Print one minified JSON document on stdout and a newline.
pub fn emit(
    a: Allocator,
    outcome: Outcome,
    env: Map,
    cached: bool,
    strict: bool,
    shell: bool,
    exit_code: u8,
) void {
    const doc = decisionView(a, outcome, env, cached, strict, shell, exit_code) catch oom();
    const line = json.dumps(a, doc, 0, true) catch oom();
    defer a.free(line);
    cli.put(1, line);
    cli.put(1, "\n");
}
