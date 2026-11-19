//! Text rules of the guard: name and path validators, one-line messages,
//! shell quoting. Values are raw bytes, as the environment holds them; where
//! the Python guard works on characters it decoded UTF-8 with
//! surrogateescape, so an invalid byte counts as one character here too.
const std = @import("std");
const common = @import("zig_common");

const Allocator = std.mem.Allocator;

// Python's text handling, shared with the other tools through zig-common.
const pytext = common.pytext;
/// One character of a byte string as Python's UTF-8 decoder sees it: its
/// code point, or null for a byte that is not valid UTF-8, and its length.
pub const Char = pytext.Char;
/// The next character of a byte string; an invalid byte is a character of
/// its own, as surrogateescape decodes it.
pub const nextChar = pytext.nextChar;
/// bytes.decode("utf-8", "replace"): each ill-formed run becomes one U+FFFD.
pub const decodeReplace = pytext.decodeReplace;
/// str.isspace for the characters str.strip() removes.
pub const pyWhitespace = pytext.pyWhitespace;
/// str.strip(): the bytes without leading and trailing Unicode whitespace.
pub const pyStrip = pytext.pyStrip;
/// str.split() with no separator: the runs between Unicode whitespace.
pub const pySplit = pytext.pySplit;
/// shlex.quote: the value as it is when a shell reads it as one word,
/// single-quoted otherwise.
pub const shellQuote = pytext.shellQuote;

/// MAX_MODULE_NAME: the longest module name the guard loads, in bytes.
pub const max_module_name = 256;
/// PROLOG_LINE_MAX: the longest line, newline included, the guard prints for
/// slurmstepd. Slurm 17.11 reads 4094 bytes of a TaskProlog line and the
/// rest as a line of its own, which a longer value could turn into another
/// directive.
pub const prolog_line_max = 4094;

fn isAlnumU(c: u8) bool {
    return std.ascii.isAlphanumeric(c) or c == '_';
}

/// ENV_NAME_RE: ^[A-Za-z_][A-Za-z0-9_]*$
pub fn envName(s: []const u8) bool {
    return common.env.validName(s);
}

/// _MOD_SEGMENT: [A-Za-z0-9_][A-Za-z0-9._+-]*
fn modSegment(s: []const u8) bool {
    if (s.len == 0 or !isAlnumU(s[0])) return false;
    for (s[1..]) |c| if (!(isAlnumU(c) or c == '.' or c == '+' or c == '-')) return false;
    return true;
}

/// MODULE_NAME_RE (two or more segments) or YEAR_MODULE_RE (four digits),
/// at most 256 bytes. The Python pattern is ASCII-only, so bytes and
/// characters agree.
pub fn moduleName(s: []const u8) bool {
    if (s.len == 0 or s.len > max_module_name) return false;
    if (s.len == 4 and allDigits(s)) return true;
    var it = std.mem.splitScalar(u8, s, '/');
    var n: usize = 0;
    while (it.next()) |seg| : (n += 1) if (!modSegment(seg)) return false;
    return n >= 2;
}

fn allDigits(s: []const u8) bool {
    for (s) |c| if (!std.ascii.isDigit(c)) return false;
    return s.len > 0;
}

/// EESSI_MODULE_RE: `^EESSI/<segment>$`
pub fn eessiModule(s: []const u8) bool {
    return std.mem.startsWith(u8, s, "EESSI/") and modSegment(s["EESSI/".len..]);
}

/// ACCEL_RE: ^accel/(?:nvidia/cc[0-9]+|amd/gfx[0-9a-f]+)$
pub fn accelSubdir(s: []const u8) bool {
    if (std.mem.startsWith(u8, s, "accel/nvidia/cc")) return allDigits(s["accel/nvidia/cc".len..]);
    if (std.mem.startsWith(u8, s, "accel/amd/gfx")) {
        const rest = s["accel/amd/gfx".len..];
        if (rest.len == 0) return false;
        for (rest) |c| if (!(std.ascii.isDigit(c) or (c >= 'a' and c <= 'f'))) return false;
        return true;
    }
    return false;
}

/// safe_rel_subdir: relative, no empty, '.', '..' or backslash; each
/// segment [A-Za-z0-9_.+-]+.
pub fn safeRelSubdir(s: []const u8) bool {
    if (s.len == 0 or s[0] == '/' or std.mem.findScalar(u8, s, '\\') != null) return false;
    var it = std.mem.splitScalar(u8, s, '/');
    while (it.next()) |seg| {
        if (seg.len == 0 or std.mem.eql(u8, seg, ".") or std.mem.eql(u8, seg, "..")) return false;
        for (seg) |c| if (!(isAlnumU(c) or c == '.' or c == '+' or c == '-')) return false;
    }
    return true;
}

/// safe_absolute: starts with '/', no newline, carriage return or NUL, no
/// '..' segment.
pub fn safeAbsolute(s: []const u8) bool {
    if (s.len == 0 or s[0] != '/' or hasLineBreakOrNul(s)) return false;
    var it = std.mem.splitScalar(u8, s, '/');
    while (it.next()) |seg| if (std.mem.eql(u8, seg, "..")) return false;
    return true;
}

/// Whether `s` holds a newline, a carriage return or a NUL byte.
pub fn hasLineBreakOrNul(s: []const u8) bool {
    return std.mem.findAny(u8, s, "\n\r\x00") != null;
}

/// _SAFE_COMPONENT_RE: ^[A-Za-z0-9._-]{1,64}$
pub fn safeComponent(s: []const u8) bool {
    if (s.len == 0 or s.len > 64) return false;
    for (s) |c| if (!(isAlnumU(c) or c == '.' or c == '-')) return false;
    return true;
}

/// value_emit_ok: whether a value can be published. A NUL byte never can; a
/// newline or carriage return only when `multiline` is set, for the quoted
/// values of the shell output.
pub fn valueEmitOk(v: []const u8, multiline: bool) bool {
    if (std.mem.findScalar(u8, v, 0) != null) return false;
    if (!multiline and std.mem.findAny(u8, v, "\n\r") != null) return false;
    return true;
}

const never_emit = [_][]const u8{
    "_",             "SHLVL",        "PWD",       "OLDPWD",        "TMPDIR",
    "BASH_ENV",      "ENV",          "SHELLOPTS", "BASHOPTS",      "BASH_XTRACEFD",
    "PS4",           "BASH_ARGC",    "BASH_ARGV", "BASH_LINENO",   "BASH_SOURCE",
    "BASH_VERSINFO", "BASH_VERSION", "DIRSTACK",  "EPOCHREALTIME", "EPOCHSECONDS",
    "FUNCNAME",      "GROUPS",       "HOSTNAME",  "HOSTTYPE",      "IFS",
    "MACHTYPE",      "OPTERR",       "OPTIND",    "OSTYPE",        "PIPESTATUS",
    "PPID",
};

/// SOFT_DROP: the prompt variables a delta leaves out when their value cannot
/// be published. Any other such value stops the reload; a prompt with a
/// newline in it must not.
pub const soft_drop = [_][]const u8{ "PS1", "PS2", "PS3", "PROMPT_COMMAND" };
/// STARTUP_VARS: the variables that make bash read a file or trace, which
/// `envmap.stripStartup` removes from the environment of the shells the
/// guard starts.
pub const startup_vars = [_][]const u8{ "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD", "PS4" };

/// Whether `s` is one of the strings in `list`.
pub fn inList(list: []const []const u8, s: []const u8) bool {
    for (list) |x| if (std.mem.eql(u8, x, s)) return true;
    return false;
}

/// _never_touch: NEVER_EMIT, BASH_FUNC_*, SLURM_*.
pub fn neverTouch(name: []const u8) bool {
    return inList(&never_emit, name) or std.mem.startsWith(u8, name, "BASH_FUNC_") or std.mem.startsWith(u8, name, "SLURM_");
}

/// _never_cache: the never-touch list and the loader's LD_*.
pub fn neverCache(name: []const u8) bool {
    return neverTouch(name) or std.mem.startsWith(u8, name, "LD_");
}

// ---- characters -------------------------------------------------------

/// Format `parts` as a single bounded line into caller-owned `out` without
/// allocating: controls (< 32 or 127) become spaces, leading and trailing
/// Python whitespace is stripped, more than `limit` characters are cut to
/// limit-3 plus "...", invalid UTF-8 bytes become '?' when `replace_invalid`
/// is set, and the result stops at a character boundary within `max_bytes`.
pub fn formatBoundedParts(
    out: []u8,
    parts: []const []const u8,
    limit: usize,
    replace_invalid: bool,
    max_bytes: usize,
) []const u8 {
    const cap = @min(out.len, max_bytes);
    const keep_prefix = if (limit >= 3) limit - 3 else 0;
    var pos: usize = 0;
    var prefix_pos: usize = 0;
    var last_non_ws_pos: usize = 0;
    var chars_kept: usize = 0;
    var started = false;
    var overflow_non_ws = false;
    var cap_cut = false;

    for (parts) |part| {
        var i: usize = 0;
        while (i < part.len) {
            const ch = nextChar(part[i..]);
            const is_ctrl = ch.cp != null and (ch.cp.? < 32 or ch.cp.? == 127);
            const is_ws = is_ctrl or (ch.cp != null and pyWhitespace(ch.cp.?));
            if (!started) {
                if (is_ws) {
                    i += ch.len;
                    continue;
                }
                started = true;
            }
            if (chars_kept < limit) {
                if (chars_kept == keep_prefix) prefix_pos = pos;
                const wlen: usize = if (is_ctrl or (ch.cp == null and replace_invalid)) 1 else ch.len;
                if (pos + wlen > cap) {
                    cap_cut = true;
                    break;
                }
                if (is_ctrl) {
                    out[pos] = ' ';
                } else if (ch.cp == null and replace_invalid) {
                    out[pos] = '?';
                } else {
                    @memcpy(out[pos .. pos + ch.len], part[i .. i + ch.len]);
                }
                pos += wlen;
                chars_kept += 1;
                if (!is_ws) last_non_ws_pos = pos;
            } else {
                if (!is_ws) {
                    overflow_non_ws = true;
                    break;
                }
            }
            i += ch.len;
        }
        if (overflow_non_ws or cap_cut) break;
    }

    if (overflow_non_ws and !cap_cut and prefix_pos + 3 <= cap) {
        @memcpy(out[prefix_pos .. prefix_pos + 3], "...");
        return out[0 .. prefix_pos + 3];
    }
    return out[0..last_non_ws_pos];
}

/// one_line: controls (below 32 and 127) become spaces, the result is
/// stripped, and more than `limit` characters are cut to limit-3 plus "...".
pub fn oneLine(gpa: Allocator, s: []const u8, limit: usize) Allocator.Error![]u8 {
    if (limit <= 500) {
        var buf: [2048]u8 = undefined;
        return gpa.dupe(u8, formatBoundedParts(&buf, &.{s}, limit, false, std.math.maxInt(usize)));
    }
    const max_bytes = @min(s.len, limit *| 4) +| 3;
    const buf = try gpa.alloc(u8, max_bytes);
    defer gpa.free(buf);
    return gpa.dupe(u8, formatBoundedParts(buf, &.{s}, limit, false, std.math.maxInt(usize)));
}

/// message_bytes: one_line(text, 500), encoded as UTF-8 with an invalid
/// byte written as '?', cut at a character boundary to fit after "print "
/// in a 4094-byte prolog line.
pub fn messageBytes(gpa: Allocator, text: []const u8) Allocator.Error![]u8 {
    var buf: [2048]u8 = undefined;
    const room = prolog_line_max - "print ".len - 1;
    return gpa.dupe(u8, formatBoundedParts(&buf, &.{text}, 500, true, room));
}

/// repr() of the str surrogateescape decodes from `s`: in single quotes, or
/// double ones when `s` holds a ' and no ", with a backslash, that quote,
/// the characters Python does not print up to U+00FF and an invalid byte
/// escaped. Past U+00FF every character is kept, where repr() also escapes
/// the ones Python does not print, such as U+2028.
pub fn pyRepr(gpa: Allocator, s: []const u8) Allocator.Error![]u8 {
    const quote: u8 = if (std.mem.findScalar(u8, s, '\'') != null and std.mem.findScalar(u8, s, '"') == null) '"' else '\'';
    var out: std.ArrayList(u8) = .empty;
    errdefer out.deinit(gpa);
    try out.append(gpa, quote);
    var i: usize = 0;
    while (i < s.len) {
        const ch = nextChar(s[i..]);
        if (ch.cp) |cp| switch (cp) {
            '\\' => try out.appendSlice(gpa, "\\\\"),
            '\t' => try out.appendSlice(gpa, "\\t"),
            '\n' => try out.appendSlice(gpa, "\\n"),
            '\r' => try out.appendSlice(gpa, "\\r"),
            0...8, 0x0b, 0x0c, 0x0e...0x1f, 0x7f...0xa0, 0xad => try out.print(gpa, "\\x{x:0>2}", .{cp}),
            else => {
                if (cp == quote) try out.append(gpa, '\\');
                try out.appendSlice(gpa, s[i .. i + ch.len]);
            },
        } else try out.print(gpa, "\\udc{x:0>2}", .{s[i]});
        i += ch.len;
    }
    try out.append(gpa, quote);
    return out.toOwnedSlice(gpa);
}

test "validators" {
    try std.testing.expect(moduleName("GCC/13.2.0"));
    try std.testing.expect(moduleName("2025"));
    try std.testing.expect(!moduleName("GCC"));
    try std.testing.expect(!moduleName("GCC//1"));
    try std.testing.expect(!moduleName("-x/1"));
    try std.testing.expect(eessiModule("EESSI/2023.06"));
    try std.testing.expect(!eessiModule("EESSI/2023/06"));
    try std.testing.expect(accelSubdir("accel/nvidia/cc90"));
    try std.testing.expect(accelSubdir("accel/amd/gfx90a"));
    try std.testing.expect(!accelSubdir("accel/amd/gfx90A"));
    try std.testing.expect(!accelSubdir("accel/nvidia/cc"));
    try std.testing.expect(safeRelSubdir("x86_64/amd/zen4"));
    try std.testing.expect(!safeRelSubdir("x86_64//zen4"));
    try std.testing.expect(!safeRelSubdir("../x"));
    try std.testing.expect(safeAbsolute("/cvmfs/x"));
    try std.testing.expect(!safeAbsolute("/a/../b"));
    try std.testing.expect(safeComponent("login1.local"));
    try std.testing.expect(!safeComponent("a b"));
    try std.testing.expect(neverCache("LD_PRELOAD") and neverTouch("SLURM_JOB_ID") and !neverTouch("LD_PRELOAD"));
    try std.testing.expect(envName("_OK_1") and !envName("") and !envName("1BAD") and !envName("A=B") and !envName("A\nB") and !envName("A\x00B"));
}

test "oneLine and messageBytes" {
    const gpa = std.testing.allocator;
    const a = try oneLine(gpa, "  a\nb\x7f\x00c  ", 500);
    defer gpa.free(a);
    try std.testing.expectEqualStrings("a b  c", a);
    const b = try oneLine(gpa, "abcdefgh", 5);
    defer gpa.free(b);
    try std.testing.expectEqualStrings("ab...", b);
    const c = try oneLine(gpa, "\u{00a0}x\xff\u{3000}", 500);
    defer gpa.free(c);
    try std.testing.expectEqualStrings("x\xff", c);
    const m = try messageBytes(gpa, "x\xffy\u{e9}");
    defer gpa.free(m);
    try std.testing.expectEqualStrings("x?y\u{e9}", m);
}

test "formatBoundedParts handles trailing whitespace and byte cap" {
    var buf: [2048]u8 = undefined;
    try std.testing.expectEqualStrings("", formatBoundedParts(&buf, &.{"  \n\t\u{3000}  "}, 5, true, 2048));
    try std.testing.expectEqualStrings("...", formatBoundedParts(&buf, &.{"x"}, 0, true, 2048));
    try std.testing.expectEqualStrings("abcde", formatBoundedParts(&buf, &.{"abcde   "}, 5, true, 2048));
    try std.testing.expectEqualStrings("ab...", formatBoundedParts(&buf, &.{"abcd  x"}, 5, true, 2048));
    try std.testing.expectEqualStrings("ab...", formatBoundedParts(&buf, &.{"ab   c"}, 5, true, 2048));
    try std.testing.expectEqualStrings("a ...", formatBoundedParts(&buf, &.{"a c  d"}, 5, true, 2048));
    try std.testing.expectEqualStrings("prefix:   hello world", formatBoundedParts(&buf, &.{ "prefix: ", "  hello\nworld  " }, 500, true, 2048));
    try std.testing.expectEqualStrings("a", formatBoundedParts(&buf, &.{"a\u{e9}b"}, 500, true, 2));

    var emoji: [2000]u8 = undefined;
    for (0..500) |j| @memcpy(emoji[j * 4 .. j * 4 + 4], "\u{1f600}");
    const fit = formatBoundedParts(&buf, &.{&emoji}, 500, true, prolog_line_max - "print ".len - 1);
    try std.testing.expectEqual(@as(usize, 2000), fit.len);
}

fn expectPyRepr(gpa: Allocator, s: []const u8, want: []const u8) !void {
    const got = try pyRepr(gpa, s);
    defer gpa.free(got);
    try std.testing.expectEqualStrings(want, got);
}

test "pyRepr quotes and escapes as repr() does" {
    const cases = [_][2][]const u8{
        .{ "/tmp/x", "'/tmp/x'" },
        .{ "it's", "\"it's\"" },
        .{ "a'b\"c", "'a\\'b\"c'" },
        .{ "a\\b", "'a\\\\b'" },
        .{ "\t\n\r\x01\x7f", "'\\t\\n\\r\\x01\\x7f'" },
        .{ "caf\xe9", "'caf\\udce9'" },
        .{ "caf\u{e9}", "'caf\u{e9}'" },
        .{ "\u{a0}\u{ad}\u{85}\u{ff}", "'\\xa0\\xad\\x85\u{ff}'" },
    };
    for (cases) |cs| try expectPyRepr(std.testing.allocator, cs[0], cs[1]);
    try std.testing.checkAllAllocationFailures(std.testing.allocator, expectPyRepr, .{ "it's\xe9", "\"it's\\udce9\"" });
}

test "decodeReplace uses maximal subparts" {
    const gpa = std.testing.allocator;
    const r = try decodeReplace(gpa, "a\xe2\x82b\xff\xf0\x9f\x98");
    defer gpa.free(r);
    try std.testing.expectEqualStrings("a\u{FFFD}b\u{FFFD}\u{FFFD}", r);
}

test "pyStrip and pySplit use Unicode whitespace" {
    try std.testing.expectEqualStrings("a b", pyStrip("\u{3000} a b\u{a0}\n"));
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const parts = try pySplit(arena_state.allocator(), "avx\u{a0}waitpkg  x\xff");
    try std.testing.expectEqual(@as(usize, 3), parts.len);
    try std.testing.expectEqualStrings("waitpkg", parts[1]);
}

test "shellQuote" {
    const gpa = std.testing.allocator;
    const cases = [_][2][]const u8{
        .{ "", "''" },
        .{ "/a:b", "/a:b" },
        .{ "a b", "'a b'" },
        .{ "it's", "'it'\"'\"'s'" },
        .{ "\xe9", "'\xe9'" },
    };
    for (cases) |cs| {
        const q = try shellQuote(gpa, cs[0]);
        defer gpa.free(q);
        try std.testing.expectEqualStrings(cs[1], q);
    }
}
