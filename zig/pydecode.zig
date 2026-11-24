//! Decode one `--restore` request the way CPython 3.14 does, and report the
//! same exception text the Python guard writes.
//!
//! The request is `sys.stdin.buffer.read().decode("utf-8")`, then
//! `json.loads` with `parse_constant` rejecting NaN, Infinity, and
//! -Infinity. A value that is not an object fails on `.get` with an
//! AttributeError. `json.loads` accepts arrays and objects nested past the
//! depth `json.parse` allows, so a document that parser refuses is read
//! again without that cap.
const std = @import("std");
const common = @import("zig_common");
const text = @import("text.zig");

const json = common.json;
const Allocator = std.mem.Allocator;

pub const Loaded = union(enum) {
    /// One stderr line, including the `eessi-arch-guard: restore failed: ` prefix.
    failure: []const u8,
    object: json.Value,
};

const Kind = enum { object, array, string, int, float, boolean, none };

const Hit = struct { kind: Kind, end: usize };

/// Decoder message, in the order `_json.c` raises them.
const JMsg = enum {
    control,
    unterminated,
    escape,
    hex,
    property,
    colon,
    comma,
    trail_object,
    trail_array,
    value,
    extra,
    bom,

    fn text(self: JMsg) []const u8 {
        return switch (self) {
            .control => "Invalid control character at",
            .unterminated => "Unterminated string starting at",
            .escape => "Invalid \\escape",
            .hex => "Invalid \\uXXXX escape",
            .property => "Expecting property name enclosed in double quotes",
            .colon => "Expecting ':' delimiter",
            .comma => "Expecting ',' delimiter",
            .trail_object => "Illegal trailing comma before end of object",
            .trail_array => "Illegal trailing comma before end of array",
            .value => "Expecting value",
            .extra => "Extra data",
            .bom => "Unexpected UTF-8 BOM (decode using utf-8-sig)",
        };
    }
};

const Scan = union(enum) {
    ok: Hit,
    json: struct { msg: JMsg, pos: usize },
    constant: []const u8,
    stop: usize,
};

fn jerr(msg: JMsg, pos: usize) Scan {
    return .{ .json = .{ .msg = msg, .pos = pos } };
}

fn ok(kind: Kind, end: usize) Scan {
    return .{ .ok = .{ .kind = kind, .end = end } };
}

const Reason = enum { start, cont, unexpected };

fn reasonText(reason: Reason) []const u8 {
    return switch (reason) {
        .start => "invalid start byte",
        .cont => "invalid continuation byte",
        .unexpected => "unexpected end of data",
    };
}

/// What a lead byte starts, matching CPython's utf-8 decoder.
/// `n` is 0 for an invalid start. `lo` and `hi` bound the second byte.
const Lead = struct { n: u8, lo: u8, hi: u8 };

const lead_table: [256]Lead = blk: {
    var t: [256]Lead = undefined;
    for (0..256) |i| {
        const b: u8 = @intCast(i);
        t[i] = if (b < 0x80)
            .{ .n = 1, .lo = 0, .hi = 0 }
        else if (b < 0xC2)
            .{ .n = 0, .lo = 0, .hi = 0 }
        else if (b < 0xE0)
            .{ .n = 2, .lo = 0x80, .hi = 0xBF }
        else if (b == 0xE0)
            .{ .n = 3, .lo = 0xA0, .hi = 0xBF }
        else if (b < 0xED)
            .{ .n = 3, .lo = 0x80, .hi = 0xBF }
        else if (b == 0xED)
            .{ .n = 3, .lo = 0x80, .hi = 0x9F }
        else if (b < 0xF0)
            .{ .n = 3, .lo = 0x80, .hi = 0xBF }
        else if (b == 0xF0)
            .{ .n = 4, .lo = 0x90, .hi = 0xBF }
        else if (b < 0xF4)
            .{ .n = 4, .lo = 0x80, .hi = 0xBF }
        else if (b == 0xF4)
            .{ .n = 4, .lo = 0x80, .hi = 0x8F }
        else
            .{ .n = 0, .lo = 0, .hi = 0 };
    }
    break :blk t;
};

const Fault = struct { start: usize, end: usize, reason: Reason };

fn isCont(b: u8) bool {
    return b >= 0x80 and b < 0xC0;
}

fn secondOk(b: u8, lead: Lead) bool {
    return b >= lead.lo and b <= lead.hi;
}

/// The first strict utf-8 error, or null when `s` is valid.
/// Spans follow `unicode_decode_utf8_impl`: a continuation error of return
/// code `ch` covers `ch - 1` bytes from the lead, and an unexpected end
/// covers the bytes still in the buffer.
fn utf8Fault(s: []const u8) ?Fault {
    var i: usize = 0;
    while (i < s.len) {
        const lead = lead_table[s[i]];
        if (lead.n == 1) {
            i += 1;
            continue;
        }
        if (lead.n == 0) return .{ .start = i, .end = i + 1, .reason = .start };
        if (lead.n == 2) {
            if (s.len - i < 2) return .{ .start = i, .end = s.len, .reason = .unexpected };
            if (!secondOk(s[i + 1], lead)) return .{ .start = i, .end = i + 1, .reason = .cont };
            i += 2;
            continue;
        }
        if (lead.n == 3) {
            if (s.len - i < 3) {
                if (s.len - i < 2) return .{ .start = i, .end = s.len, .reason = .unexpected };
                if (!secondOk(s[i + 1], lead)) return .{ .start = i, .end = i + 1, .reason = .cont };
                return .{ .start = i, .end = s.len, .reason = .unexpected };
            }
            if (!secondOk(s[i + 1], lead)) return .{ .start = i, .end = i + 1, .reason = .cont };
            if (!isCont(s[i + 2])) return .{ .start = i, .end = i + 2, .reason = .cont };
            i += 3;
            continue;
        }
        if (s.len - i < 4) {
            if (s.len - i < 2) return .{ .start = i, .end = s.len, .reason = .unexpected };
            if (!secondOk(s[i + 1], lead)) return .{ .start = i, .end = i + 1, .reason = .cont };
            if (s.len - i < 3) return .{ .start = i, .end = s.len, .reason = .unexpected };
            if (!isCont(s[i + 2])) return .{ .start = i, .end = i + 2, .reason = .cont };
            return .{ .start = i, .end = s.len, .reason = .unexpected };
        }
        if (!secondOk(s[i + 1], lead)) return .{ .start = i, .end = i + 1, .reason = .cont };
        if (!isCont(s[i + 2])) return .{ .start = i, .end = i + 2, .reason = .cont };
        if (!isCont(s[i + 3])) return .{ .start = i, .end = i + 3, .reason = .cont };
        i += 4;
    }
    return null;
}

fn failLine(a: Allocator, body: []const u8) Allocator.Error![]u8 {
    const cleaned = try text.oneLine(a, body, 300);
    return a.print("eessi-arch-guard: restore failed: {s}\n", .{cleaned});
}

fn utf8Line(a: Allocator, s: []const u8, fault: Fault) Allocator.Error![]u8 {
    const body = if (fault.end == fault.start + 1)
        try a.print("'utf-8' codec can't decode byte 0x{x:0>2} in position {d}: {s}", .{
            s[fault.start], fault.start, reasonText(fault.reason),
        })
    else
        try a.print("'utf-8' codec can't decode bytes in position {d}-{d}: {s}", .{
            fault.start, fault.end - 1, reasonText(fault.reason),
        });
    return failLine(a, body);
}

fn codePoints(a: Allocator, s: []const u8) Allocator.Error![]u21 {
    var out: std.ArrayList(u21) = .empty;
    var i: usize = 0;
    while (i < s.len) {
        const n = std.unicode.utf8ByteSequenceLength(s[i]) catch unreachable;
        const cp = std.unicode.utf8Decode(s[i..][0..n]) catch unreachable;
        try out.append(a, cp);
        i += n;
    }
    return out.items;
}

const LineCol = struct { line: usize, col: usize };

/// `JSONDecodeError`: line is `count('\\n', 0, pos) + 1`, and the column is
/// `pos - rfind('\\n', 0, pos)` (a missing newline yields column `pos + 1`).
fn lineCol(cps: []const u21, pos: usize) LineCol {
    var line: usize = 1;
    var start: usize = 0;
    const end = @min(pos, cps.len);
    var i: usize = 0;
    while (i < end) : (i += 1) {
        if (cps[i] == '\n') {
            line += 1;
            start = i + 1;
        }
    }
    return .{ .line = line, .col = pos - start + 1 };
}

fn positioned(a: Allocator, msg: JMsg, cps: []const u21, pos: usize) Allocator.Error![]u8 {
    const lc = lineCol(cps, pos);
    return a.print("{s}: line {d} column {d} (char {d})", .{ msg.text(), lc.line, lc.col, pos });
}

fn attrBody(kind: Kind) []const u8 {
    return switch (kind) {
        .array => "'list' object has no attribute 'get'",
        .string => "'str' object has no attribute 'get'",
        .int => "'int' object has no attribute 'get'",
        .float => "'float' object has no attribute 'get'",
        .boolean => "'bool' object has no attribute 'get'",
        .none => "'NoneType' object has no attribute 'get'",
        .object => unreachable,
    };
}

fn isWs(c: u21) bool {
    return c == ' ' or c == '\t' or c == '\n' or c == '\r';
}

fn skipWs(s: []const u21, i: usize) usize {
    var j = i;
    while (j < s.len and isWs(s[j])) j += 1;
    return j;
}

fn isDigit(c: u21) bool {
    return c >= '0' and c <= '9';
}

fn isHex(c: u21) bool {
    return isDigit(c) or (c >= 'a' and c <= 'f') or (c >= 'A' and c <= 'F');
}

fn hexValue(c: u21) u21 {
    return switch (c) {
        '0'...'9' => c - '0',
        'a'...'f' => c - 'a' + 10,
        'A'...'F' => c - 'A' + 10,
        else => unreachable,
    };
}

fn startsWith(s: []const u21, i: usize, lit: []const u8) bool {
    if (i + lit.len > s.len) return false;
    for (lit, 0..) |ch, k| if (s[i + k] != ch) return false;
    return true;
}

/// `scanstring_unicode`. `from` is the index of the first character after
/// the opening quote. A high surrogate joins a following `\u` low surrogate
/// only when `end + 6 < len`, as the C decoder requires.
fn scanString(s: []const u21, from: usize) Scan {
    const len = s.len;
    const begin = from - 1;
    var end = from;
    while (true) {
        var next = end;
        var c: u21 = 0;
        var special = false;
        while (next < len) {
            c = s[next];
            if (c == '"' or c == '\\') {
                special = true;
                break;
            }
            if (c <= 0x1f) return jerr(.control, next);
            next += 1;
        }
        if (!special) return jerr(.unterminated, begin);
        if (c == '"') return ok(.string, next + 1);
        next += 1;
        if (next == len) return jerr(.unterminated, begin);
        c = s[next];
        if (c != 'u') {
            end = next + 1;
            switch (c) {
                '"', '\\', '/', 'b', 'f', 'n', 'r', 't' => {},
                else => return jerr(.escape, end - 2),
            }
        } else {
            next += 1;
            end = next + 4;
            if (end >= len) return jerr(.hex, next - 1);
            var hex: u21 = 0;
            var p = next;
            while (p < end) : (p += 1) {
                if (!isHex(s[p])) return jerr(.hex, end - 5);
                hex = (hex << 4) | hexValue(s[p]);
            }
            if (hex >= 0xD800 and hex <= 0xDBFF and end + 6 < len and s[end] == '\\' and s[end + 1] == 'u') {
                const new_end = end + 6;
                var low: u21 = 0;
                var q = end + 2;
                var bad = false;
                while (q < new_end) : (q += 1) {
                    if (!isHex(s[q])) {
                        bad = true;
                        break;
                    }
                    low = (low << 4) | hexValue(s[q]);
                }
                if (bad) return jerr(.hex, new_end - 5);
                if (low >= 0xDC00 and low <= 0xDFFF) end = new_end;
            }
        }
    }
}

fn parseObject(s: []const u21, idx0: usize) Scan {
    var idx = skipWs(s, idx0);
    if (idx >= s.len or s[idx] != '}') {
        while (true) {
            if (idx >= s.len or s[idx] != '"') return jerr(.property, idx);
            switch (scanString(s, idx + 1)) {
                .ok => |hit| idx = hit.end,
                else => |other| return other,
            }
            idx = skipWs(s, idx);
            if (idx >= s.len or s[idx] != ':') return jerr(.colon, idx);
            idx += 1;
            idx = skipWs(s, idx);
            switch (scanOnce(s, idx)) {
                .ok => |hit| idx = hit.end,
                else => |other| return other,
            }
            idx = skipWs(s, idx);
            if (idx < s.len and s[idx] == '}') break;
            if (idx >= s.len or s[idx] != ',') return jerr(.comma, idx);
            const comma_idx = idx;
            idx += 1;
            idx = skipWs(s, idx);
            if (idx < s.len and s[idx] == '}') return jerr(.trail_object, comma_idx);
        }
    }
    return ok(.object, idx + 1);
}

fn parseArray(s: []const u21, idx0: usize) Scan {
    var idx = skipWs(s, idx0);
    if (idx >= s.len or s[idx] != ']') {
        while (true) {
            switch (scanOnce(s, idx)) {
                .ok => |hit| idx = hit.end,
                else => |other| return other,
            }
            idx = skipWs(s, idx);
            if (idx < s.len and s[idx] == ']') break;
            if (idx >= s.len or s[idx] != ',') return jerr(.comma, idx);
            const comma_idx = idx;
            idx += 1;
            idx = skipWs(s, idx);
            if (idx < s.len and s[idx] == ']') return jerr(.trail_array, comma_idx);
        }
    }
    if (idx >= s.len or s[idx] != ']') return jerr(.value, s.len - 1);
    return ok(.array, idx + 1);
}

/// `_match_number_unicode`. A leading zero, a trailing dot, or an exponent
/// with no digit ends the number early; the caller then reports extra data
/// or a missing delimiter. No digit at all is `StopIteration` at `start`.
fn matchNumber(s: []const u21, start: usize) Scan {
    const len = s.len;
    var idx = start;
    if (s[idx] == '-') {
        idx += 1;
        if (idx >= len) return .{ .stop = start };
    }
    if (idx < len and s[idx] >= '1' and s[idx] <= '9') {
        idx += 1;
        while (idx < len and isDigit(s[idx])) idx += 1;
    } else if (idx < len and s[idx] == '0') {
        idx += 1;
    } else return .{ .stop = start };

    // `idx < end_idx` in the C scanner: one character remains after `idx`.
    if (idx + 1 < len and s[idx] == '.' and isDigit(s[idx + 1])) {
        idx += 2;
        while (idx < len and isDigit(s[idx])) idx += 1;
    }
    if (idx + 1 < len and (s[idx] == 'e' or s[idx] == 'E')) {
        const e_start = idx;
        idx += 1;
        if (idx + 1 < len and (s[idx] == '+' or s[idx] == '-')) idx += 1;
        while (idx < len and isDigit(s[idx])) idx += 1;
        if (!isDigit(s[idx - 1])) idx = e_start;
    }
    var float = false;
    for (s[start..idx]) |c| {
        if (c == '.' or c == 'e' or c == 'E') float = true;
    }
    return ok(if (float) .float else .int, idx);
}

fn scanOnce(s: []const u21, idx: usize) Scan {
    if (idx >= s.len) return .{ .stop = idx };
    switch (s[idx]) {
        '"' => return scanString(s, idx + 1),
        '{' => return parseObject(s, idx + 1),
        '[' => return parseArray(s, idx + 1),
        'n' => if (startsWith(s, idx, "null")) return ok(.none, idx + 4),
        't' => if (startsWith(s, idx, "true")) return ok(.boolean, idx + 4),
        'f' => if (startsWith(s, idx, "false")) return ok(.boolean, idx + 5),
        'N' => if (startsWith(s, idx, "NaN")) return .{ .constant = "NaN" },
        'I' => if (startsWith(s, idx, "Infinity")) return .{ .constant = "Infinity" },
        '-' => if (startsWith(s, idx, "-Infinity")) return .{ .constant = "-Infinity" },
        else => {},
    }
    return matchNumber(s, idx);
}

/// One JSON value, with no depth cap. Strings and numbers are slices of `s`,
/// as `json.parse` leaves them. Used only after that parser has refused a
/// document the scanner already accepted.
const Deep = struct {
    s: []const u8,
    i: usize = 0,
    arena: Allocator,

    fn ws(p: *Deep) void {
        while (p.i < p.s.len and (p.s[p.i] == ' ' or p.s[p.i] == '\t' or p.s[p.i] == '\n' or p.s[p.i] == '\r')) p.i += 1;
    }

    fn lit(p: *Deep, l: []const u8) bool {
        if (std.mem.startsWith(u8, p.s[p.i..], l)) {
            p.i += l.len;
            return true;
        }
        return false;
    }

    fn value(p: *Deep) json.ParseError!json.Value {
        p.ws();
        if (p.i >= p.s.len) return error.Syntax;
        switch (p.s[p.i]) {
            'n' => return if (p.lit("null")) .null else error.Syntax,
            't' => return if (p.lit("true")) .{ .bool = true } else error.Syntax,
            'f' => return if (p.lit("false")) .{ .bool = false } else error.Syntax,
            '"' => return .{ .string = try p.string() },
            '[' => {
                p.i += 1;
                var items: std.ArrayList(json.Value) = .empty;
                p.ws();
                if (p.i < p.s.len and p.s[p.i] == ']') {
                    p.i += 1;
                    return .{ .array = &.{} };
                }
                while (true) {
                    try items.append(p.arena, try p.value());
                    p.ws();
                    if (p.i >= p.s.len) return error.Syntax;
                    if (p.s[p.i] == ',') {
                        p.i += 1;
                        continue;
                    }
                    if (p.s[p.i] == ']') {
                        p.i += 1;
                        return .{ .array = items.items };
                    }
                    return error.Syntax;
                }
            },
            '{' => {
                p.i += 1;
                var members: std.ArrayList(json.Member) = .empty;
                p.ws();
                if (p.i < p.s.len and p.s[p.i] == '}') {
                    p.i += 1;
                    return .{ .object = &.{} };
                }
                while (true) {
                    p.ws();
                    if (p.i >= p.s.len or p.s[p.i] != '"') return error.Syntax;
                    const key = try p.string();
                    p.ws();
                    if (p.i >= p.s.len or p.s[p.i] != ':') return error.Syntax;
                    p.i += 1;
                    const v = try p.value();
                    try members.append(p.arena, .{ .key = key, .value = v });
                    p.ws();
                    if (p.i >= p.s.len) return error.Syntax;
                    if (p.s[p.i] == ',') {
                        p.i += 1;
                        continue;
                    }
                    if (p.s[p.i] == '}') {
                        p.i += 1;
                        return .{ .object = members.items };
                    }
                    return error.Syntax;
                }
            },
            '-', '0'...'9' => {
                const start = p.i;
                if (p.s[p.i] == '-') p.i += 1;
                const digits = p.i;
                while (p.i < p.s.len and std.ascii.isDigit(p.s[p.i])) p.i += 1;
                if (p.i == digits) return error.Syntax;
                if (p.s[digits] == '0' and p.i - digits > 1) return error.Syntax;
                if (p.i < p.s.len and p.s[p.i] == '.') {
                    p.i += 1;
                    const f = p.i;
                    while (p.i < p.s.len and std.ascii.isDigit(p.s[p.i])) p.i += 1;
                    if (p.i == f) return error.Syntax;
                }
                if (p.i < p.s.len and (p.s[p.i] == 'e' or p.s[p.i] == 'E')) {
                    p.i += 1;
                    if (p.i < p.s.len and (p.s[p.i] == '+' or p.s[p.i] == '-')) p.i += 1;
                    const e = p.i;
                    while (p.i < p.s.len and std.ascii.isDigit(p.s[p.i])) p.i += 1;
                    if (p.i == e) return error.Syntax;
                }
                return .{ .number = p.s[start..p.i] };
            },
            else => return error.Syntax,
        }
    }

    fn string(p: *Deep) json.ParseError![]const u8 {
        p.i += 1;
        const start = p.i;
        while (p.i < p.s.len) {
            const c = p.s[p.i];
            if (c == '"') {
                const raw = p.s[start..p.i];
                p.i += 1;
                return raw;
            }
            if (c < 0x20) return error.Syntax;
            if (c == '\\') {
                if (p.i + 1 >= p.s.len) return error.Syntax;
                switch (p.s[p.i + 1]) {
                    '"', '\\', '/', 'b', 'f', 'n', 'r', 't' => p.i += 2,
                    'u' => {
                        if (p.i + 6 > p.s.len) return error.Syntax;
                        const hex = p.s[p.i + 2 .. p.i + 6];
                        for (hex) |h| if (!std.ascii.isHex(h)) return error.Syntax;
                        p.i += 6;
                    },
                    else => return error.Syntax,
                }
                continue;
            }
            p.i += 1;
        }
        return error.Syntax;
    }
};

fn parseDeep(arena: Allocator, s: []const u8) json.ParseError!json.Value {
    var p: Deep = .{ .s = s, .arena = arena };
    const v = try p.value();
    p.ws();
    if (p.i != s.len) return error.Syntax;
    return v;
}

/// Classify `input` as the Python `--restore` helper does.
/// `error.OutOfMemory` is the only failure; a rejected request is `.failure`.
pub fn load(a: Allocator, input: []const u8) Allocator.Error!Loaded {
    if (utf8Fault(input)) |fault| return .{ .failure = try utf8Line(a, input, fault) };
    const cps = try codePoints(a, input);
    if (cps.len > 0 and cps[0] == 0xFEFF) {
        return .{ .failure = try failLine(a, try positioned(a, .bom, cps, 0)) };
    }
    const start = skipWs(cps, 0);
    switch (scanOnce(cps, start)) {
        .stop => |pos| return .{ .failure = try failLine(a, try positioned(a, .value, cps, pos)) },
        .json => |err| return .{ .failure = try failLine(a, try positioned(a, err.msg, cps, err.pos)) },
        .constant => |name| {
            const body = try a.print("{s} is not JSON", .{name});
            return .{ .failure = try failLine(a, body) };
        },
        .ok => |hit| {
            const end = skipWs(cps, hit.end);
            if (end != cps.len) return .{ .failure = try failLine(a, try positioned(a, .extra, cps, end)) };
            if (hit.kind != .object) return .{ .failure = try failLine(a, attrBody(hit.kind)) };
            const value = json.parse(a, input) catch |err| switch (err) {
                error.OutOfMemory => return error.OutOfMemory,
                error.Syntax => parseDeep(a, input) catch |deep_err| switch (deep_err) {
                    error.OutOfMemory => return error.OutOfMemory,
                    error.Syntax => return .{ .failure = try failLine(a, "the request is not JSON") },
                },
            };
            return .{ .object = value };
        },
    }
}

const Vector = struct {
    name: []const u8,
    input_hex: []const u8,
    code: u8,
    stdout: []const u8,
    stderr: []const u8,
    exc: ?[]const u8,
};

const vectors_json = @embedFile("testdata/restore_decode_vectors.json");

fn unhex(a: Allocator, hex: []const u8) ![]u8 {
    if (hex.len % 2 != 0) return error.BadHex;
    var out: std.ArrayList(u8) = .empty;
    var i: usize = 0;
    while (i < hex.len) : (i += 2) {
        const byte = std.fmt.parseInt(u8, hex[i..][0..2], 16) catch return error.BadHex;
        try out.append(a, byte);
    }
    return out.items;
}

test "restore decode vectors match CPython" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const vectors = try std.json.parseFromSliceLeaky([]Vector, a, vectors_json, .{});
    try std.testing.expect(vectors.len >= 40);

    var saw_array_31 = false;
    var saw_array_32 = false;
    var saw_array_33 = false;
    var saw_object_31 = false;
    var saw_object_32 = false;
    var saw_object_33 = false;
    for (vectors) |vector| {
        if (std.mem.eql(u8, vector.name, "array-depth-31")) saw_array_31 = true;
        if (std.mem.eql(u8, vector.name, "array-depth-32")) saw_array_32 = true;
        if (std.mem.eql(u8, vector.name, "array-depth-33")) saw_array_33 = true;
        if (std.mem.eql(u8, vector.name, "object-depth-31")) saw_object_31 = true;
        if (std.mem.eql(u8, vector.name, "object-depth-32")) saw_object_32 = true;
        if (std.mem.eql(u8, vector.name, "object-depth-33")) saw_object_33 = true;
        const input = try unhex(a, vector.input_hex);
        const loaded = try load(a, input);
        switch (loaded) {
            .object => {
                if (vector.code != 0) {
                    std.debug.print("vector {s}: got an object, want code {d}\n", .{ vector.name, vector.code });
                    return error.TestExpectedEqual;
                }
            },
            .failure => |msg| {
                if (vector.code != 1 or !std.mem.eql(u8, msg, vector.stderr)) {
                    std.debug.print("vector {s}\n got: {s}want: {s}\n", .{ vector.name, msg, vector.stderr });
                    return error.TestExpectedEqual;
                }
            },
        }
    }
    try std.testing.expect(saw_array_31 and saw_array_32 and saw_array_33);
    try std.testing.expect(saw_object_31 and saw_object_32 and saw_object_33);
}
