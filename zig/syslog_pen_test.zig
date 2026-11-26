const std = @import("std");
const envmap = @import("envmap.zig");
const syslog = @import("syslog.zig");

fn settingsWithPen(a: std.mem.Allocator, pen: []const u8) !std.StringArrayHashMapUnmanaged([]const u8) {
    var st: std.StringArrayHashMapUnmanaged([]const u8) = .empty;
    try st.put(a, "syslog_pen", pen);
    return st;
}

test "the default PEN is 32473" {
    try std.testing.expectEqualStrings("32473", syslog.default_pen);
}

test "the environment PEN wins over the config PEN without a warning" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var env: envmap.Map = .empty;
    try env.put(a, "EESSI_ARCH_GUARD_SYSLOG_PEN", "49152");
    const r = syslog.resolve(a, null, env, try settingsWithPen(a, "12345"));
    try std.testing.expectEqualStrings("49152", r.pen);
    try std.testing.expect(r.pen_warning == null);
}

test "the config PEN is used when the environment has none" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const env: envmap.Map = .empty;
    const r = syslog.resolve(a, null, env, try settingsWithPen(a, "12345"));
    try std.testing.expectEqualStrings("12345", r.pen);
    try std.testing.expect(r.pen_warning == null);
}

test "an invalid environment PEN warns and keeps the config PEN" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var env: envmap.Map = .empty;
    try env.put(a, "EESSI_ARCH_GUARD_SYSLOG_PEN", "0");
    const r = syslog.resolve(a, null, env, try settingsWithPen(a, "12345"));
    try std.testing.expectEqualStrings("12345", r.pen);
    try std.testing.expect(r.pen_warning != null);
}
