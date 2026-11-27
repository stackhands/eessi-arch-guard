const std = @import("std");
const m = @import("main.zig");

fn contains(hay: []const u8, needle: []const u8) bool {
    return std.mem.indexOf(u8, hay, needle) != null;
}

test "submitTarget names a partition value with trailing whitespace" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var settings: m.Settings = .empty;
    try settings.put(a, "submit_target.partition.cpu", "x86_64/amd/zen4 ");
    try settings.put(a, "submit_target.constraint.zen4", "x86_64/amd/zen4\n");
    var why: []const u8 = "";
    try std.testing.expect(m.submitTarget(a, settings, "cpu", "", &why) == null);
    try std.testing.expect(contains(why, "invalid target"));
    try std.testing.expect(contains(why, "x86_64/amd/zen4 "));
}

test "submitTarget names a constraint value with a line feed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var settings: m.Settings = .empty;
    try settings.put(a, "submit_target.partition.cpu", "x86_64/amd/zen4");
    try settings.put(a, "submit_target.constraint.zen4", "x86_64/amd/zen4\n");
    var why: []const u8 = "";
    try std.testing.expect(m.submitTarget(a, settings, "cpu", "zen4", &why) == null);
    try std.testing.expect(contains(why, "invalid target"));
    try std.testing.expect(contains(why, "x86_64/amd/zen4"));
}

test "submitTarget names an unmapped partition" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var settings: m.Settings = .empty;
    try settings.put(a, "submit_target.partition.cpu", "x86_64/amd/zen4");
    try settings.put(a, "submit_target.constraint.zen4", "x86_64/amd/zen4");
    try settings.put(a, "submit_target.constraint.zen2", "x86_64/amd/zen2");
    var why: []const u8 = "";
    try std.testing.expect(m.submitTarget(a, settings, "missing", "", &why) == null);
    try std.testing.expect(contains(why, "no submit_target.partition.missing"));
    const agreed = m.submitTarget(a, settings, "cpu", "zen4", &why);
    try std.testing.expectEqualStrings("x86_64/amd/zen4", agreed.?);
    try std.testing.expect(m.submitTarget(a, settings, "cpu", "zen2", &why) == null);
    try std.testing.expect(contains(why, "different targets"));
}

test "check-submit options are opt in" {
    const plain = try m.parseArgs(std.testing.allocator, &.{});
    try std.testing.expect(!plain.check_submit);
    try std.testing.expect(!plain.json);
    try std.testing.expect(plain.partition == null);
    try std.testing.expect(plain.constraint == null);
    try std.testing.expect(plain.eb_stack == null);
    const args = try m.parseArgs(std.testing.allocator, &.{ "--check-submit", "--partition", "cpu", "--constraint=zen4", "--eb-stack", "/opt/bin/eb-stack" });
    try std.testing.expect(args.check_submit);
    try std.testing.expectEqualStrings("cpu", args.partition.?);
    try std.testing.expectEqualStrings("zen4", args.constraint.?);
    try std.testing.expectEqualStrings("/opt/bin/eb-stack", args.eb_stack.?);
    try std.testing.expect((try m.parseArgs(std.testing.allocator, &.{ "--check-submit", "--json" })).json);
}
