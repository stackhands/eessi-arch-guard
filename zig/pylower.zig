//! str.lower() for the bytes of a string, as Python lower-cases a cpuinfo
//! field name. ASCII letters map by arithmetic. Past ASCII the mapping is the
//! table in pylower_table.zig, which tools/gen_pylower.py reads off Python
//! 3.9. Its Unicode 13.0.0 is the one every supported Python shares, so a
//! code point that a later Unicode adds is left as it is, as Python 3.9
//! leaves it. In the table a code point may lower-case to another letter
//! (U+212A, the Kelvin sign, to `k`) or to two code points (U+0130 to `i`
//! and U+0307), and the capital sigma gives U+03C2 at the end of a word and
//! U+03C3 elsewhere. U+017F, the long s, is already lower case and stays as
//! it is; only a case-insensitive regular expression reads it as an s.
const std = @import("std");
const common = @import("zig_common");
const table = @import("pylower_table.zig");

const pytext = common.pytext;
const Allocator = std.mem.Allocator;

const capital_sigma: u21 = 0x3A3;
const final_sigma: u21 = 0x3C2;
const medial_sigma: u21 = 0x3C3;

/// `s.decode("utf-8", "replace").lower().encode("utf-8")`: an ill-formed run
/// is one U+FFFD, as it is when Python decodes a cpuinfo before it reads a
/// field name. The result is allocated from `gpa`.
pub fn lower(gpa: Allocator, s: []const u8) Allocator.Error![]u8 {
    if (isAscii(s)) {
        const out = try gpa.alloc(u8, s.len);
        for (s, 0..) |ch, i| out[i] = std.ascii.toLower(ch);
        return out;
    }
    const decoded = try pytext.decodeReplace(gpa, s);
    defer gpa.free(decoded);
    var points: std.ArrayList(u21) = .empty;
    defer points.deinit(gpa);
    var at: usize = 0;
    while (at < decoded.len) {
        const ch = pytext.nextChar(decoded[at..]);
        try points.append(gpa, ch.cp.?);
        at += ch.len;
    }
    var out: std.ArrayList(u8) = .empty;
    errdefer out.deinit(gpa);
    for (points.items, 0..) |cp, i| {
        const mapped = lowerPoint(points.items, i, cp);
        for (mapped.slice()) |to| try appendPoint(&out, gpa, to);
    }
    return out.toOwnedSlice(gpa);
}

fn isAscii(s: []const u8) bool {
    for (s) |ch| if (ch >= 0x80) return false;
    return true;
}

const Lowered = struct {
    points: [2]u21,
    len: u2,

    fn slice(self: *const Lowered) []const u21 {
        return self.points[0..self.len];
    }
};

fn lowerPoint(points: []const u21, i: usize, cp: u21) Lowered {
    if (cp < 0x80) return one(std.ascii.toLower(@intCast(cp)));
    if (cp == capital_sigma) return one(if (isFinalSigma(points, i)) final_sigma else medial_sigma);
    if (findRun(cp)) |run| return one(@intCast(@as(i32, cp) + run.delta));
    for (table.lower_special) |special| {
        if (special.cp == cp) return .{ .points = special.to, .len = 2 };
    }
    return one(cp);
}

fn one(cp: u21) Lowered {
    return .{ .points = .{ cp, 0 }, .len = 1 };
}

/// The run of the table that holds `cp`, if `cp` is one of its members.
fn findRun(cp: u21) ?table.Run {
    var lo: usize = 0;
    var hi: usize = table.lower_runs.len;
    while (lo < hi) {
        const mid = lo + (hi - lo) / 2;
        const run = table.lower_runs[mid];
        if (cp < run.first) {
            hi = mid;
        } else if (cp > run.last) {
            lo = mid + 1;
        } else return if ((cp - run.first) % run.stride == 0) run else null;
    }
    return null;
}

fn inRanges(ranges: []const [2]u21, cp: u21) bool {
    var lo: usize = 0;
    var hi: usize = ranges.len;
    while (lo < hi) {
        const mid = lo + (hi - lo) / 2;
        if (cp < ranges[mid][0]) {
            hi = mid;
        } else if (cp > ranges[mid][1]) {
            lo = mid + 1;
        } else return true;
    }
    return false;
}

/// U+03A3 is word-final when a cased letter comes before it, with only
/// case-ignorable code points between, and none comes after it by the same
/// rule. That is how CPython's handle_capital_sigma reads it.
fn isFinalSigma(points: []const u21, i: usize) bool {
    var before = i;
    const cased_before = while (before > 0) {
        before -= 1;
        if (inRanges(&table.case_ignorable, points[before])) continue;
        break inRanges(&table.cased, points[before]);
    } else false;
    if (!cased_before) return false;
    for (points[i + 1 ..]) |cp| {
        if (inRanges(&table.case_ignorable, cp)) continue;
        return !inRanges(&table.cased, cp);
    }
    return true;
}

fn appendPoint(out: *std.ArrayList(u8), gpa: Allocator, cp: u21) Allocator.Error!void {
    var buf: [4]u8 = undefined;
    const n = std.unicode.utf8Encode(cp, &buf) catch unreachable;
    try out.appendSlice(gpa, buf[0..n]);
}

fn expectLower(want: []const u8, input: []const u8) !void {
    const got = try lower(std.testing.allocator, input);
    defer std.testing.allocator.free(got);
    try std.testing.expectEqualStrings(want, got);
}

test "lower maps ASCII letters and leaves the rest of ASCII" {
    try expectLower("key_1-x", "KEY_1-x");
    try expectLower("", "");
}

test "lower gives the Kelvin sign as k and the dotted capital I as i and a dot" {
    try expectLower("key", "\u{212A}ey");
    try expectLower("kk", "k\u{212A}");
    try expectLower("i\u{307}sa", "\u{130}SA");
}

test "lower leaves the long s and the dotless i as they are" {
    try expectLower("\u{17F}ys", "\u{17F}YS");
    try expectLower("\u{131}sa", "\u{131}SA");
}

test "lower maps letters of other scripts" {
    try expectLower("\u{e9}cole", "\u{C9}COLE");
    try expectLower("\u{aB70}", "\u{13A0}");
    try expectLower("\u{10D0}", "\u{1C90}");
    try expectLower("\u{10428}", "\u{10400}");
    try expectLower("\u{1c6}", "\u{1C5}");
}

test "lower follows Unicode 13.0.0 where a later version differs, as Python 3.9 does" {
    // U+10570, a Vithkuqi capital, and U+2C2F, a Glagolitic one, came in
    // Unicode 14.0.0 and lower-case only on Python 3.11 and later.
    try expectLower("\u{10570}", "\u{10570}");
    try expectLower("\u{2C2F}", "\u{2C2F}");
    // U+1734 is case-ignorable in Unicode 13.0.0 and not from 14.0.0 on, so
    // the sigma after it is final here and on Python 3.9.
    try expectLower("a\u{1734}\u{3C2}", "A\u{1734}\u{3A3}");
}

test "lower writes a capital sigma as final or medial as Python does" {
    try expectLower("\u{3b1}\u{3c2}", "\u{391}\u{3A3}");
    try expectLower("\u{3c3}", "\u{3A3}");
    try expectLower("\u{3c3}\u{3b1}", "\u{3A3}\u{391}");
    try expectLower("\u{3b1}\u{3c3}\u{3b1}", "\u{391}\u{3A3}\u{391}");
    try expectLower("\u{3b1}.\u{3c2}", "\u{391}.\u{3A3}");
    try expectLower("\u{3b1}.\u{3c3}\u{3b1}", "\u{391}.\u{3A3}\u{391}");
    try expectLower("\u{3b1}_\u{3c3}", "\u{391}_\u{3A3}");
    try expectLower("\u{3b1}\u{3c3}\u{3c3}\u{3b1}", "\u{391}\u{3A3}\u{3A3}\u{391}");
    try expectLower("\u{3b1}\u{3c3}\u{3c2}", "\u{391}\u{3A3}\u{3A3}");
}

test "lower reads an ill-formed run as one replacement character" {
    try expectLower("a\u{FFFD}b", "A\xff" ++ "B");
    try expectLower("\u{FFFD}", "\xe2\x82");
}

test "the table runs are sorted, apart, and find exactly their members" {
    var previous_last: u21 = 0x7F;
    var member_count: usize = 0;
    for (table.lower_runs) |run| {
        try std.testing.expect(run.first > previous_last);
        try std.testing.expect(run.first <= run.last);
        try std.testing.expect(run.stride == 1 or run.stride == 2);
        try std.testing.expect((run.last - run.first) % run.stride == 0);
        previous_last = run.last;
        var cp = run.first;
        while (cp <= run.last) : (cp += run.stride) {
            try std.testing.expectEqual(run, findRun(cp).?);
            member_count += 1;
        }
    }
    var found: usize = 0;
    var cp: u21 = 0x80;
    while (cp <= 0x10FFFF) : (cp += 1) {
        if (findRun(cp) != null) found += 1;
    }
    try std.testing.expectEqual(member_count, found);
}

test "the sigma classes are sorted, apart, and found by their ends" {
    inline for (.{ table.cased, table.case_ignorable }) |ranges| {
        var previous_last: ?u21 = null;
        for (ranges) |range| {
            try std.testing.expect(range[0] <= range[1]);
            if (previous_last) |last| try std.testing.expect(range[0] > last + 1);
            previous_last = range[1];
            try std.testing.expect(inRanges(&ranges, range[0]));
            try std.testing.expect(inRanges(&ranges, range[1]));
            if (range[0] > 0) try std.testing.expect(!inRanges(&ranges, range[0] - 1));
            try std.testing.expect(!inRanges(&ranges, range[1] + 1));
        }
    }
}
