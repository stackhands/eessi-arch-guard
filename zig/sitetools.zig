//! The two site tools: `--check-mount` says whether the EESSI repository is on
//! a CernVM-FS mount and whether the site module directory precedes the EESSI
//! one on MODULEPATH; `--check-revision` reads the client revision from
//! `cvmfs_config stat` output and compares it with a minimum. Neither changes
//! the environment or a mount.
const std = @import("std");
const common = @import("zig_common");
const text = @import("text.zig");
const envmap = @import("envmap.zig");

const sys = common.sys;
const fsx = common.fsx;
const Allocator = std.mem.Allocator;
const cli = common.cli;

pub const default_repo = "/cvmfs/software.eessi.io";
pub const default_mountinfo = "/proc/self/mountinfo";
/// The largest `cvmfs_config stat` output that is read.
pub const stat_read_max: usize = 1 << 20;
/// The largest mountinfo that is read.
pub const mountinfo_read_max: usize = 64 << 20;

/// Why a file could not be read: the text after "cannot read PATH: ".
pub const ReadFailure = []const u8;

/// The contents of a regular file. A FIFO or directory is refused before a
/// read, so a hostile path cannot block the tool. `failure` receives the
/// reason when the result is null.
pub fn readRegular(arena: Allocator, path: []const u8, max: usize, failure: *ReadFailure) ?[]u8 {
    const fd = sys.openat(fsx.cwd, path, .{ .NONBLOCK = true, .CLOEXEC = true }, 0) catch |err| {
        failure.* = sys.describe(err);
        return null;
    };
    defer sys.close(fd);
    const st = fsx.fstat(fd) catch |err| {
        failure.* = sys.describe(err);
        return null;
    };
    if (!st.isReg()) {
        failure.* = "not a regular file";
        return null;
    }
    return fsx.readAll(arena, fd, max) catch |err| {
        failure.* = if (err == error.StreamTooLong)
            "file too large"
        else if (sys.asError(err)) |se| sys.describe(se) else "out of memory";
        return null;
    };
}

/// Surrounding whitespace removed, repeated slashes collapsed to one and a
/// trailing slash dropped (except for "/" itself).
pub fn norm(arena: Allocator, path: []const u8) Allocator.Error![]const u8 {
    const stripped = common.pytext.pyStrip(path);
    var out: std.ArrayList(u8) = .empty;
    for (stripped) |c| {
        if (c == '/' and out.items.len > 0 and out.items[out.items.len - 1] == '/') continue;
        try out.append(arena, c);
    }
    while (out.items.len > 1 and out.items[out.items.len - 1] == '/') out.items.len -= 1;
    return out.items;
}

/// mountinfo writes space, tab, newline and backslash as \ooo; an escape
/// that is not three octal digits up to \377 stays as written.
pub fn unescape(arena: Allocator, raw: []const u8) Allocator.Error![]const u8 {
    if (std.mem.findScalar(u8, raw, '\\') == null) return raw;
    var out: std.ArrayList(u8) = .empty;
    var i: usize = 0;
    while (i < raw.len) {
        if (raw[i] == '\\' and i + 3 < raw.len) {
            const d = raw[i + 1 .. i + 4];
            const octal = d[0] >= '0' and d[0] <= '7' and d[1] >= '0' and d[1] <= '7' and d[2] >= '0' and d[2] <= '7';
            if (octal) {
                const v = (@as(u32, d[0] - '0') << 6) | (@as(u32, d[1] - '0') << 3) | @as(u32, d[2] - '0');
                if (v <= 0o377) {
                    try out.append(arena, @intCast(v));
                    i += 4;
                    continue;
                }
            }
        }
        try out.append(arena, raw[i]);
        i += 1;
    }
    return out.items;
}

pub const Mount = struct { target: []const u8, fstype: []const u8 };

/// The last, longest mount that holds `path`.
pub fn holdingMount(arena: Allocator, mountinfo: []const u8, path_in: []const u8) Allocator.Error!?Mount {
    const path = try norm(arena, path_in);
    var best: ?Mount = null;
    var lines = text.splitLines(mountinfo);
    while (lines.next()) |line| {
        const cut = std.mem.find(u8, line, " - ") orelse continue;
        const head = line[0..cut];
        const tail = line[cut + 3 ..];
        const fields = try common.pytext.pySplit(arena, head);
        if (fields.len < 6) continue;
        const target = try norm(arena, try unescape(arena, fields[4]));
        const parts = try common.pytext.pySplit(arena, tail);
        if (parts.len == 0) continue;
        const inside = std.mem.eql(u8, target, "/") or std.mem.eql(u8, path, target) or
            (path.len > target.len and std.mem.startsWith(u8, path, target) and path[target.len] == '/');
        if (!inside) continue;
        if (best == null or target.len >= best.?.target.len) best = .{ .target = target, .fstype = parts[0] };
    }
    return best;
}

/// The file system type when `repo` is on a CernVM-FS mount.
pub fn clientMount(arena: Allocator, mountinfo: []const u8, repo: []const u8) Allocator.Error!?[]const u8 {
    const held = (try holdingMount(arena, mountinfo, repo)) orelse return null;
    if (std.mem.find(u8, held.fstype, "cvmfs") == null) return null;
    return held.fstype;
}

pub const Order = enum { both_missing, site_missing, eessi_missing, site_first, eessi_first, same };

/// Where the site module directory sits relative to the EESSI one.
pub fn order(arena: Allocator, modulepath: []const u8, site: []const u8, eessi: []const u8) Allocator.Error!Order {
    const site_n = try norm(arena, site);
    const eessi_n = try norm(arena, eessi);
    var site_at: ?usize = null;
    var eessi_at: ?usize = null;
    var index: usize = 0;
    var it = std.mem.splitScalar(u8, modulepath, ':');
    while (it.next()) |raw| {
        const item = try norm(arena, raw);
        if (item.len == 0) continue;
        if (site_at == null and std.mem.eql(u8, item, site_n)) site_at = index;
        if (eessi_at == null and std.mem.eql(u8, item, eessi_n)) eessi_at = index;
        index += 1;
    }
    if (site_at == null and eessi_at == null) return .both_missing;
    if (site_at == null) return .site_missing;
    if (eessi_at == null) return .eessi_missing;
    if (site_at.? < eessi_at.?) return .site_first;
    if (site_at.? > eessi_at.?) return .eessi_first;
    return .same;
}

pub const Report = struct { status: u8, text: []const u8 };

/// The verdict lines and the exit status: 0 when the repository is on a
/// CernVM-FS mount and the site modules come first, else 1.
pub fn report(arena: Allocator, mountinfo: []const u8, modulepath: []const u8, repo: []const u8, site: []const u8, eessi: []const u8) Allocator.Error!Report {
    var out: std.ArrayList(u8) = .empty;
    var failed = false;
    if (try clientMount(arena, mountinfo, repo)) |kind| {
        try out.print(arena, "ok   {s} is on a cvmfs mount ({s})\n", .{ repo, kind });
    } else {
        const held = try holdingMount(arena, mountinfo, repo);
        const where: []const u8 = if (held) |h| h.fstype else "no mount";
        try out.print(arena, "FAIL {s} is not on a cvmfs mount ({s})\n", .{ repo, where });
        failed = true;
    }
    const site_n = try norm(arena, site);
    const eessi_n = try norm(arena, eessi);
    switch (try order(arena, modulepath, site, eessi)) {
        .site_first => try out.print(arena, "ok   site modules {s} precede {s}\n", .{ site_n, eessi_n }),
        .eessi_first => {
            try out.print(arena, "FAIL site modules {s} are behind {s}\n", .{ site_n, eessi_n });
            failed = true;
        },
        .site_missing => {
            try out.print(arena, "FAIL site modules {s} are not on MODULEPATH\n", .{site_n});
            failed = true;
        },
        .eessi_missing => {
            try out.print(arena, "FAIL EESSI modules {s} are not on MODULEPATH\n", .{eessi_n});
            failed = true;
        },
        .both_missing => {
            try out.print(arena, "FAIL neither {s} nor {s} is on MODULEPATH\n", .{ site_n, eessi_n });
            failed = true;
        },
        .same => {
            try out.print(arena, "FAIL site modules {s} and the EESSI modules are the same path\n", .{site_n});
            failed = true;
        },
    }
    return .{ .status = if (failed) 1 else 0, .text = out.items };
}

/// A module directory for an install prefix: the prefix itself when it
/// already ends in modules/all.
pub fn modulesDir(arena: Allocator, value: []const u8) Allocator.Error![]const u8 {
    const v = try norm(arena, value);
    if (std.mem.eql(u8, v, "/")) return "/modules/all";
    if (std.mem.endsWith(u8, v, "/modules/all")) return v;
    return std.fmt.allocPrint(arena, "{s}/modules/all", .{v});
}

/// True when a shell expanded an unset install variable to /modules/all.
pub fn placeholderModules(arena: Allocator, value: []const u8) Allocator.Error!bool {
    return std.mem.eql(u8, try norm(arena, value), "/modules/all");
}

/// The first `Revision: N` line of `cvmfs_config stat` output. A line may say
/// `Catalog revision` or `Repository revision`, in any case; a leading
/// byte order mark is ignored. A number that does not fit 64 bits is not a
/// revision.
pub fn revisionOf(data: []const u8) ?u64 {
    var body = data;
    if (std.mem.startsWith(u8, body, "\xef\xbb\xbf")) body = body[3..];
    var lines = text.splitLines(body);
    while (lines.next()) |raw| {
        const line = common.pytext.pyStrip(raw);
        const rest = labelRest(line) orelse continue;
        var i: usize = 0;
        while (i < rest.len and isSpace(rest[i])) i += 1;
        if (i >= rest.len or rest[i] != ':') continue;
        i += 1;
        while (i < rest.len and isSpace(rest[i])) i += 1;
        const start = i;
        while (i < rest.len and rest[i] >= '0' and rest[i] <= '9') i += 1;
        if (i == start) continue;
        const digits = rest[start..i];
        while (i < rest.len and isSpace(rest[i])) i += 1;
        if (i != rest.len) continue;
        return std.fmt.parseInt(u64, digits, 10) catch continue;
    }
    return null;
}

fn isSpace(c: u8) bool {
    return c == ' ' or c == '\t' or c == '\n' or c == '\r' or c == 0x0b or c == 0x0c;
}

/// `line` after one of the three labels, matched without regard to case.
fn labelRest(line: []const u8) ?[]const u8 {
    const labels = [_][]const u8{ "catalog revision", "repository revision", "revision" };
    for (labels) |label| {
        if (line.len >= label.len and std.ascii.eqlIgnoreCase(line[0..label.len], label)) return line[label.len..];
    }
    return null;
}

pub const Gate = struct { status: u8, text: []const u8 };

/// Exit status and message: `minimum` null reports `found`; a smaller
/// `found` is behind (1); no revision is 2.
pub fn gate(arena: Allocator, found: ?u64, minimum: ?u64) Allocator.Error!Gate {
    const f = found orelse return .{ .status = 2, .text = "eessi-arch-guard: no revision in the stat output\n" };
    if (minimum) |m| {
        if (f < m) return .{ .status = 1, .text = try std.fmt.allocPrint(arena, "behind revision {d} is older than {d}\n", .{ f, m }) };
    }
    return .{ .status = 0, .text = try std.fmt.allocPrint(arena, "revision {d}\n", .{f}) };
}

pub const MountArgs = struct {
    repo: ?[]const u8 = null,
    site: ?[]const u8 = null,
    eessi: ?[]const u8 = null,
    modulepath: ?[]const u8 = null,
    mountinfo: ?[]const u8 = null,
    require_input: bool = false,
};

fn say(fd: sys.fd_t, arena: Allocator, comptime fmt: []const u8, args: anytype) void {
    const msg = std.fmt.allocPrint(arena, fmt, args) catch return;
    cli.put(fd, msg);
}

fn envOr(env: envmap.Map, name: []const u8) []const u8 {
    return env.get(name) orelse "";
}

/// `--check-mount`: the exit status, with the report on stdout. Status 2 is
/// a usage or read error, named on stderr.
pub fn runMount(arena: Allocator, a: MountArgs, env: envmap.Map) u8 {
    return runMountImpl(arena, a, env) catch {
        say(2, arena, "eessi-arch-guard: out of memory\n", .{});
        return 2;
    };
}

fn runMountImpl(arena: Allocator, a: MountArgs, env: envmap.Map) Allocator.Error!u8 {
    const env_repo = envOr(env, "EESSI_CVMFS_REPO");
    const repo: []const u8 = a.repo orelse (if (env_repo.len > 0) env_repo else default_repo);
    const modulepath: []const u8 = a.modulepath orelse envOr(env, "MODULEPATH");
    const site_raw = if (a.site != null and a.site.?.len > 0) a.site.? else envOr(env, "EESSI_SITE_SOFTWARE_PATH");
    const eessi_raw = if (a.eessi != null and a.eessi.?.len > 0) a.eessi.? else envOr(env, "EESSI_SOFTWARE_PATH");
    var site = common.pytext.pyStrip(site_raw);
    var eessi = common.pytext.pyStrip(eessi_raw);
    if (site.len == 0 or eessi.len == 0) {
        say(2, arena, "eessi-arch-guard: --check-mount needs --site and --eessi, or EESSI_SITE_SOFTWARE_PATH and EESSI_SOFTWARE_PATH\n", .{});
        return 2;
    }
    if (a.require_input and
        (try placeholderModules(arena, site) or try placeholderModules(arena, eessi) or
            try placeholderModules(arena, try modulesDir(arena, site)) or
            try placeholderModules(arena, try modulesDir(arena, eessi))))
    {
        say(2, arena, "eessi-arch-guard: --require-input: install paths are missing (unset shell variables expand to /modules/all)\n", .{});
        return 2;
    }
    if (a.site == null or a.site.?.len == 0) site = try modulesDir(arena, site);
    if (a.eessi == null or a.eessi.?.len == 0) eessi = try modulesDir(arena, eessi);
    const path: []const u8 = if (a.mountinfo != null and a.mountinfo.?.len > 0) a.mountinfo.? else default_mountinfo;
    var why: ReadFailure = "";
    const data = readRegular(arena, path, mountinfo_read_max, &why) orelse {
        say(2, arena, "eessi-arch-guard: cannot read {s}: {s}\n", .{ path, why });
        return 2;
    };
    const r = try report(arena, data, modulepath, repo, site, eessi);
    cli.put(1, r.text);
    return r.status;
}

/// `--check-revision`: reads `cvmfs_config stat` output from `stat`, and
/// with `min` fails when the revision is older. Status 2 is a usage or read
/// error or no revision in the text.
pub fn runRevision(arena: Allocator, stat: ?[]const u8, min: ?[]const u8) u8 {
    return runRevisionImpl(arena, stat, min) catch {
        say(2, arena, "eessi-arch-guard: out of memory\n", .{});
        return 2;
    };
}

fn parseMin(arena: Allocator, raw: []const u8) Allocator.Error!union(enum) { value: u64, failure: []const u8 } {
    var t = std.mem.trim(u8, raw, " \t\n\r\x0b\x0c");
    if (t.len > 0 and (t[0] == '+' or t[0] == '-')) {
        if (t[0] == '-') {
            const digits = std.mem.trim(u8, t[1..], " ");
            _ = std.fmt.parseInt(u64, digits, 10) catch return .{ .failure = try std.fmt.allocPrint(arena, "--min needs an integer, got '{s}'", .{raw}) };
            return .{ .failure = "--min must be zero or more" };
        }
        t = t[1..];
    }
    const v = std.fmt.parseInt(u64, t, 10) catch return .{ .failure = try std.fmt.allocPrint(arena, "--min needs an integer, got '{s}'", .{raw}) };
    return .{ .value = v };
}

fn runRevisionImpl(arena: Allocator, stat: ?[]const u8, min: ?[]const u8) Allocator.Error!u8 {
    var minimum: ?u64 = null;
    if (min) |m| switch (try parseMin(arena, m)) {
        .value => |v| minimum = v,
        .failure => |msg| {
            say(2, arena, "eessi-arch-guard: {s}\n", .{msg});
            return 2;
        },
    };
    const path = stat orelse {
        say(2, arena, "eessi-arch-guard: --check-revision needs --stat FILE\n", .{});
        return 2;
    };
    var why: ReadFailure = "";
    const data = readRegular(arena, path, stat_read_max, &why) orelse {
        say(2, arena, "eessi-arch-guard: cannot read {s}: {s}\n", .{ path, why });
        return 2;
    };
    const g = try gate(arena, revisionOf(data), minimum);
    cli.put(if (g.status == 0) 1 else 2, g.text);
    return g.status;
}

test "unescape keeps an out-of-range or non-octal escape as written" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    try std.testing.expectEqualStrings("\\777", try unescape(a, "\\777"));
    try std.testing.expectEqualStrings("a b", try unescape(a, "a\\040b"));
    try std.testing.expectEqualStrings("bad\\800path name", try unescape(a, "bad\\800path\\040name"));
}

test "a cvmfs mount holds the repository and the root disk does not" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const mount =
        "36 35 0:36 / / rw,relatime - ext4 /dev/sda1 rw\n" ++
        "47 36 0:47 / /cvmfs/software.eessi.io rw,nosuid,nodev - fuse.cvmfs software.eessi.io rw\n";
    try std.testing.expectEqualStrings("fuse.cvmfs", (try clientMount(a, mount, "/cvmfs/software.eessi.io")).?);
    try std.testing.expectEqualStrings("fuse.cvmfs", (try clientMount(a, mount, "/cvmfs//software.eessi.io")).?);
    try std.testing.expect((try clientMount(a, mount, "/cvmfs/other.example")) == null);
    const plain = "36 35 0:36 / / rw - ext4 /dev/sda1 rw\n";
    try std.testing.expect((try clientMount(a, plain, "/cvmfs/software.eessi.io")) == null);
}

test "an escaped mount point is found by its unescaped name" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const mountinfo =
        "36 35 0:36 / / rw,relatime - ext4 /dev/sda1 rw\n" ++
        "48 36 0:48 / /mnt/bad\\800path\\040name rw - ext4 /dev/sdb1 rw\n";
    const got = (try holdingMount(a, mountinfo, "/mnt/bad\\800path name/sub")).?;
    try std.testing.expectEqualStrings("/mnt/bad\\800path name", got.target);
    try std.testing.expectEqualStrings("ext4", got.fstype);
}

test "the site path has to precede the EESSI path" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const site = "/opt/site/modules/all";
    const eessi = "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/amd/zen4/modules/all";
    try std.testing.expectEqual(Order.site_first, try order(a, site ++ ":" ++ eessi, site, eessi));
    try std.testing.expectEqual(Order.eessi_first, try order(a, eessi ++ ":" ++ site, site, eessi));
    try std.testing.expectEqual(Order.site_first, try order(a, site ++ "/:" ++ eessi ++ "/", site, eessi));
    try std.testing.expectEqual(Order.site_first, try order(a, "/opt/site//modules/all:" ++ eessi, site, eessi));
    try std.testing.expectEqual(Order.site_missing, try order(a, eessi, site, eessi));
    try std.testing.expectEqual(Order.eessi_missing, try order(a, site, site, eessi));
    try std.testing.expectEqual(Order.site_missing, try order(a, "/opt/site-extra/modules/all:" ++ eessi, site, eessi));
    try std.testing.expectEqual(Order.same, try order(a, site, site, site));
    try std.testing.expectEqual(Order.both_missing, try order(a, "/x", site, eessi));
}

test "revisionOf reads the three labels and ignores the rest" {
    try std.testing.expectEqual(@as(?u64, 42), revisionOf("Revision: 42\nExpires in: 4 minutes\n"));
    try std.testing.expectEqual(@as(?u64, 7), revisionOf("Catalog revision: 7\n"));
    try std.testing.expectEqual(@as(?u64, 9), revisionOf("repository REVISION :  9  \n"));
    try std.testing.expectEqual(@as(?u64, 42), revisionOf("\xef\xbb\xbfRevision: 42\n"));
    try std.testing.expectEqual(@as(?u64, 42), revisionOf("Comment: \xff\xfe\nRevision: 42\n"));
    try std.testing.expect(revisionOf("Connected via proxy DIRECT\n") == null);
    try std.testing.expect(revisionOf("Revision: 4x\n") == null);
}

test "gate reports, compares and names a missing revision" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var g = try gate(a, 42, null);
    try std.testing.expectEqual(@as(u8, 0), g.status);
    try std.testing.expectEqualStrings("revision 42\n", g.text);
    g = try gate(a, 42, 40);
    try std.testing.expectEqual(@as(u8, 0), g.status);
    g = try gate(a, 42, 42);
    try std.testing.expectEqual(@as(u8, 0), g.status);
    g = try gate(a, 42, 50);
    try std.testing.expectEqual(@as(u8, 1), g.status);
    try std.testing.expect(std.mem.find(u8, g.text, "behind") != null);
    g = try gate(a, null, 50);
    try std.testing.expectEqual(@as(u8, 2), g.status);
    try std.testing.expect(std.mem.find(u8, g.text, "no revision") != null);
}
