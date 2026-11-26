//! Tests of the rules that keep a value, a name or a message from becoming a
//! second directive: the grammar of module names and subdirectories, the
//! delta and the text that publishes it, the message cut, the timeout rules,
//! and how the site configuration is read and trusted.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main = @import("main.zig");
const envmap = @import("envmap.zig");
const text = @import("text.zig");

const sys = common.sys;
const fsx = common.fsx;
const Allocator = std.mem.Allocator;
const Map = envmap.Map;

test "module names are two or more safe segments, or four ASCII digits" {
    const good = [_][]const u8{ "EESSI/2025.06", "R/4.4.2-gfbf-2024a", "Compiler/GCCcore/15.2.0", "GCC/15.2.0", "foss/2024a", "2026", "1999" };
    for (good) |name| try std.testing.expect(text.moduleName(name));
    const bad = [_][]const u8{
        "R/4.4.2\nexport LD_PRELOAD=x",     "R/4.4.2;rm", "$(id)",         "`id`",
        "foo/'bar",                         "foo/\"bar",  "has space/1.0", "EESSI",
        "/EESSI/2025.06",                   "R/../4.4.2", "name/ver\r",    "name/ver$",
        "GCC",                              "202",        "20261",         "year2026",
        "\u{ff12}\u{ff10}\u{ff12}\u{ff16}", "2026/",      "2026//x",       "/2026",
        "",
    };
    for (bad) |name| try std.testing.expect(!text.moduleName(name));
    try std.testing.expect(text.eessiModule("EESSI/2025.06"));
    try std.testing.expect(!text.eessiModule("EESSI"));
}

test "a hostile character placed anywhere in a module name makes it unsafe" {
    var prng = std.Random.DefaultPrng.init(20261101);
    const rng = prng.random();
    const hostile = [_][]const u8{ "\n", "\r", "'", "\"", "$", "`", "$(touch /tmp/x)", " ", ";", "|", "&", "\\", "\x00" };
    const body = "R/4.4.2";
    var buf: [64]u8 = undefined;
    for (0..300) |_| {
        const cut = rng.uintAtMost(usize, body.len);
        const h = hostile[rng.uintLessThan(usize, hostile.len)];
        const name = try std.fmt.bufPrint(&buf, "{s}{s}{s}", .{ body[0..cut], h, body[cut..] });
        try std.testing.expect(!text.moduleName(name));
    }
}

test "a software subdirectory is relative and has no traversal" {
    try std.testing.expect(text.safeRelSubdir("x86_64/amd/zen2"));
    try std.testing.expect(!text.safeRelSubdir("../etc"));
    try std.testing.expect(!text.safeRelSubdir("/etc"));
    try std.testing.expect(!text.safeRelSubdir("x86_64/../../tmp"));
    try std.testing.expect(!text.safeRelSubdir("x86_64/amd/.."));
    try std.testing.expect(!text.safeRelSubdir(""));
}

test "an accelerator subdirectory is accel/nvidia/cc<digits> or accel/amd/gfx<hex>" {
    try std.testing.expect(text.accelSubdir("accel/nvidia/cc90"));
    try std.testing.expect(text.accelSubdir("accel/nvidia/cc120"));
    try std.testing.expect(text.accelSubdir("accel/amd/gfx90a"));
    try std.testing.expect(!text.accelSubdir("nvidia/cc90"));
    try std.testing.expect(!text.accelSubdir("accel/nvidia/cc90\nexport X=1"));
}

fn changesOf(a: Allocator, old: Map, new: Map, multiline: bool) !?envmap.Changes {
    return envmap.delta(a, old, new, multiline);
}

test "a value with a newline in prolog mode stops the whole delta" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var old: Map = .empty;
    try old.put(a, "PATH", "/old");
    var new: Map = .empty;
    try new.put(a, "PATH", "/new");
    try new.put(a, "FOO", "a\nexport LD_PRELOAD=/evil.so");
    try std.testing.expect((try changesOf(a, old, new, false)) == null);
    // The shell output quotes values, so the same delta is allowed there.
    try std.testing.expect((try changesOf(a, old, new, true)) != null);
}

test "a name with a newline stops the whole delta" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const old: Map = .empty;
    var new: Map = .empty;
    try new.put(a, "OK", "1");
    try new.put(a, "FOO\nexport LD_PRELOAD", "/evil.so");
    try std.testing.expect((try changesOf(a, old, new, false)) == null);
    try std.testing.expect((try changesOf(a, old, new, true)) == null);
}

test "a prompt variable that cannot be carried is dropped and the rest stays" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var old: Map = .empty;
    try old.put(a, "PATH", "/old");
    try old.put(a, "PS1", "old");
    var new: Map = .empty;
    try new.put(a, "PATH", "/new");
    try new.put(a, "PS1", "line\nline");
    const d = (try changesOf(a, old, new, false)).?;
    try std.testing.expectEqual(@as(usize, 1), d.count());
    try std.testing.expectEqualStrings("/new", d.get("PATH").?.?);
}

test "Slurm variables, TMPDIR and the shell's own variables are never published" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var old: Map = .empty;
    try old.put(a, "SLURM_JOB_ID", "1");
    try old.put(a, "TMPDIR", "/old");
    try old.put(a, "PATH", "/old");
    var new: Map = .empty;
    try new.put(a, "SLURM_JOB_ID", "2");
    try new.put(a, "TMPDIR", "/new");
    try new.put(a, "PATH", "/new");
    const d = (try changesOf(a, old, new, false)).?;
    try std.testing.expectEqual(@as(usize, 1), d.count());
    try std.testing.expectEqualStrings("/new", d.get("PATH").?.?);
}

test "a prolog value is written as it is" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var changes: envmap.Changes = .empty;
    try changes.put(a, "FOO", "cost $5 `id` \"q\"");
    const raw = try envmap.render(a, changes, null, false, false);
    try std.testing.expectEqualStrings("export FOO=cost $5 `id` \"q\"\n", raw);
}

test "a newline in the message cannot start another directive" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const raw = try envmap.render(a, .empty, "hello\nexport LD_PRELOAD=/evil.so", false, true);
    try std.testing.expectEqualStrings("print hello export LD_PRELOAD=/evil.so\n", raw);
    try std.testing.expectEqual(@as(usize, 1), std.mem.count(u8, raw, "\n"));
    // The shell output carries no print line at all.
    const shell = try envmap.render(a, .empty, "hello", true, true);
    try std.testing.expectEqualStrings("", shell);
}

test "an export line past the Slurm 17.11 read is refused, in prolog mode only" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const long = try a.alloc(u8, text.prolog_line_max + 8);
    @memset(long, 'a');
    var changes: envmap.Changes = .empty;
    try changes.put(a, "PATH", long);
    try std.testing.expectError(error.LineTooLong, envmap.render(a, changes, null, false, false));
    var short: envmap.Changes = .empty;
    try short.put(a, "PATH", "short");
    const shell = try envmap.render(a, short, null, true, false);
    try std.testing.expect(std.mem.find(u8, shell, "PATH") != null);
}

test "a message is cut at a character boundary to fit its line" {
    try std.testing.expectEqual(@as(usize, 4094), text.prolog_line_max);
    var buf: [256]u8 = undefined;
    const accent = "\u{e9}";
    var long_buf: [80]u8 = undefined;
    for (0..40) |i| @memcpy(long_buf[i * 2 ..][0..2], accent);
    const long: []const u8 = &long_buf;
    // Five bytes hold two whole accents; a naive cut would split the third.
    const fitted = text.formatBoundedParts(&buf, &.{long}, 500, true, 5);
    try std.testing.expect(std.unicode.utf8ValidateSlice(fitted));
    try std.testing.expect(fitted.len <= 5);
    try std.testing.expectEqualStrings("\u{e9}\u{e9}", fitted);
    // A line of 20 bytes leaves room for the message after "print " and a newline.
    const room = 20 - "print ".len - 1;
    const raw = text.formatBoundedParts(&buf, &.{long}, 500, true, room);
    try std.testing.expect(std.unicode.utf8ValidateSlice(raw));
    try std.testing.expect(raw.len <= room);
    try std.testing.expect(std.mem.endsWith(u8, raw, accent));
}

test "a timeout is a positive number of seconds at most 2147483.647" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    try std.testing.expectEqual(@as(f64, 60), main.default_timeout);
    try std.testing.expectEqualStrings("2147483.647", main.timeout_max_text);
    try std.testing.expectEqual(@as(?f64, 60.0), main.parseTimeout(a, "60"));
    try std.testing.expectEqual(@as(?f64, 1.5), main.parseTimeout(a, "1.5"));
    try std.testing.expect(main.parseTimeout(a, "0") == null);
    try std.testing.expect(main.parseTimeout(a, "-1") == null);
    try std.testing.expect(main.parseTimeout(a, "nope") == null);
    try std.testing.expectEqual(@as(?f64, main.timeout_max), main.parseTimeout(a, main.timeout_max_text));
    try std.testing.expect(main.parseTimeout(a, "4294967.294") == null);
    try std.testing.expect(main.parseTimeout(a, "2147483.648") == null);
    try std.testing.expect(main.parseTimeout(a, "9223372036") == null);
}

test "the command line timeout wins over the config and a bad one falls back with a warning" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var settings: main.Settings = .empty;
    try settings.put(a, "timeout", "12");
    const from_config = main.resolveTimeout(a, null, settings);
    try std.testing.expectEqual(@as(f64, 12), from_config.value);
    try std.testing.expect(from_config.warning == null);
    const from_cli = main.resolveTimeout(a, "3", settings);
    try std.testing.expectEqual(@as(f64, 3), from_cli.value);
    try std.testing.expect(from_cli.warning == null);
    const bad = main.resolveTimeout(a, "0", null);
    try std.testing.expectEqual(main.default_timeout, bad.value);
    try std.testing.expect(bad.warning != null);
}

test "config lines keep the original bytes of a value that is not UTF-8" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var bad: std.ArrayList([]const u8) = .empty;
    const plain = try main.parseConfigBytes(a, "# comment\nchain = /bin/true\ntimeout = 60\n", &bad);
    try std.testing.expectEqualStrings("/bin/true", plain.get("chain").?);
    try std.testing.expectEqualStrings("60", plain.get("timeout").?);
    try std.testing.expectEqual(@as(usize, 0), bad.items.len);
    const settings = try main.parseConfigBytes(a, "chain = /bin/true\n# caf\xe9\ntimeout = 1\xff\n", &bad);
    try std.testing.expectEqualStrings("/bin/true", settings.get("chain").?);
    try std.testing.expectEqual(@as(usize, 1), bad.items.len);
    try std.testing.expectEqualStrings("timeout", bad.items[0]);
    try std.testing.expectEqualStrings("1\xff", settings.get("timeout").?);
    const other = try main.parseConfigBytes(a, "other = 1\n", &bad);
    try std.testing.expect(other.get("chain") == null);
}

fn scratch(a: Allocator, name: []const u8) ![]const u8 {
    var cwd_buf: [sys.path_max]u8 = undefined;
    const here = try fsx.realpath(fsx.cwd, ".", &cwd_buf);
    const base = try std.fmt.allocPrint(a, "{s}/.zig-cache/eag-{s}-{d}", .{ here, name, linux.getpid() });
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, base)).ptr, 0o700);
    return base;
}

fn writeFile(path: []const u8, data: []const u8) !void {
    const fd = try sys.openat(fsx.cwd, path, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o600);
    defer sys.close(fd);
    try sys.writeAll(fd, data);
}

test "a config file that is not owned by root is untrusted, and a missing one names the error" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try scratch(a, "cfg-untrusted");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const path = try std.fmt.allocPrint(a, "{s}/eessi-arch-guard.conf", .{base});
    try writeFile(path, "chain = /bin/true\n");
    const reason = main.configUntrusted(a, path).?;
    try std.testing.expect(std.mem.find(u8, reason, "not owned by root") != null);
    const missing = "/nonexistent/it's\nconf";
    try std.testing.expectEqualStrings(
        "/nonexistent/it's\nconf ([Errno 2] No such file or directory: \"/nonexistent/it's\\nconf\")",
        main.configUntrusted(a, missing).?,
    );
}

test "the open config file is checked again for type, link, owner and mode" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try scratch(a, "cfg-recheck");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    var zb: [sys.path_max:0]u8 = undefined;

    // A FIFO and a directory are no config, whatever the path check said.
    const fifo = try std.fmt.allocPrint(a, "{s}/fifo.conf", .{base});
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.mknodat(fsx.cwd, (try sys.z(&zb, fifo)).ptr, 0o010000 | 0o600, 0)));
    const dir = try std.fmt.allocPrint(a, "{s}/dir.conf", .{base});
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, dir)).ptr, 0o700);
    for ([_][]const u8{ fifo, dir }) |path| {
        const cfg = main.readConfigFile(a, path);
        try std.testing.expect(cfg.settings == null);
        try std.testing.expect(cfg.warning != null);
        try std.testing.expect(std.mem.startsWith(u8, cfg.warning.?, "eessi-arch-guard: config ignored: "));
        try std.testing.expect(std.mem.find(u8, cfg.warning.?, path) != null);
    }

    // A link is not followed, even to a file that is fine.
    const target = try std.fmt.allocPrint(a, "{s}/target.conf", .{base});
    try writeFile(target, "timeout = 3\n");
    const link = try std.fmt.allocPrint(a, "{s}/link.conf", .{base});
    _ = linux.symlinkat((try sys.z(&zb, target)).ptr, linux.AT.FDCWD, (try sys.z(&zb, link)).ptr);
    const linked = main.readConfigFile(a, link);
    try std.testing.expect(linked.settings == null);
    try std.testing.expect(std.mem.startsWith(u8, linked.warning.?, "eessi-arch-guard: config ignored: [Errno 40] Too many levels of symbolic links: '"));

    // A file the process owns is not root's.
    const owned = main.readConfigFile(a, target);
    try std.testing.expect(owned.settings == null);
    const changed = try std.fmt.allocPrint(a, "eessi-arch-guard: config ignored: {s} changed while it was read", .{target});
    try std.testing.expectEqualStrings(changed, owned.warning.?);

    // On the open descriptor only root's file, not writable by group or others, is trusted.
    try std.testing.expect(main.configDescriptorTrusted(0, 0o100644));
    try std.testing.expect(main.configDescriptorTrusted(0, 0o100600));
    try std.testing.expect(!main.configDescriptorTrusted(0, 0o100666));
    try std.testing.expect(!main.configDescriptorTrusted(0, 0o100664));
    try std.testing.expect(!main.configDescriptorTrusted(0, 0o100646));
    try std.testing.expect(!main.configDescriptorTrusted(1000, 0o100644));
}
