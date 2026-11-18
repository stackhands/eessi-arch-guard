//! eessi-arch-guard: a Slurm TaskProlog that reloads the EESSI module
//! environment when the loaded CPU or accelerator target is not this
//! node's. It exits 0 on every path a prolog can take, unless --strict asks
//! for 78 on a failed decision: a panic, a crash signal or a failed write
//! prints one skip line instead of killing the task.
const std = @import("std");
const linux = std.os.linux;
const build_options = @import("build_options");
const common = @import("zig_common");
const text = @import("text.zig");
const envmap = @import("envmap.zig");
const guard = @import("guard.zig");
const jsonout = @import("jsonout.zig");
const syslog = @import("syslog.zig");

const sys = common.sys;
const fsx = common.fsx;
const proc = common.proc;
const cli = common.cli;
const json = common.json;
const Allocator = std.mem.Allocator;
const Py = json.Py;
const empty: []const u8 = "";

/// The version `--version` prints first, `.version` of build.zig.zon. The
/// Python guard's `__version__` and pyproject.toml name the same one.
pub const version = build_options.version;
const tool = "eessi-arch-guard";
/// The Python guard's bare version, then this build (COMPAT.md).
const version_text = cli.versionLine(tool, version, build_options.commit)[tool.len + 1 ..];
const strict_failure_exit: u8 = 78;
const default_timeout: f64 = 60.0;
/// TIMEOUT_MAX, as Python prints it: the Python guard's subprocess waits
/// with poll(), whose timeout is a C int of milliseconds.
const timeout_max: f64 = 2147483.647;
const timeout_max_text = "2147483.647";

const help_text =
    \\usage: eessi-arch-guard [--shell] [--strict] [--dry-run] [--json]
    \\                        [--chain PATH] [--timeout SEC] [--syslog TARGET]
    \\                        [--restore] [-h|--help] [--version]
    \\       eessi-arch-guard --check-submit --partition NAME [--constraint NAME]
    \\                        [--eb-stack PATH] [--json]
    \\
    \\Slurm TaskProlog: when the loaded EESSI CPU or accelerator target is not this
    \\node's, print export/unset lines for the reloaded module environment.
    \\--check-submit checks LOADEDMODULES against a mapped target without changing it.
    \\--partition NAME selects submit_target.partition.NAME from the site config.
    \\--constraint NAME also requires submit_target.constraint.NAME to select that target.
    \\--eb-stack PATH selects the eb-stack executable used for the read-only check.
    \\--shell prints bash for: eval "$(eessi-arch-guard --shell)"
    \\--strict returns 78 on a failed decision or an unexpected exception.
    \\--dry-run evaluates config, chain, probes, and decision without writing state.
    \\--json prints one line of JSON with schema_version 1. On a decision run it
    \\replaces prolog or shell lines; with --dry-run it also carries the diagnostic
    \\keys; with --check-submit it replaces the text summary.
    \\--chain PATH runs an existing task prolog first. Slurm does not pass arguments,
    \\so a site sets this in a wrapper or in a root-owned eessi-arch-guard.conf.
    \\--timeout SEC bounds the decision. The default is 60 seconds.
    \\--syslog TARGET sends structured RFC 5424 or journald decision events.
    \\--restore reads a JSON restore request on stdin and writes the JSON result.
    \\-h, --help print this text. --version prints the version.
    \\
;

// ---- the exit-0 guarantee ------------------------------------------------

/// Set before anything can fail: where the skip line goes, whether this
/// task prints it, and what to exit.
var emergency_shell = false;
var emergency_announce = true;
var emergency_strict = false;

fn emergencyWrite(kind: []const u8, detail: []const u8) void {
    var buf: [1024]u8 = undefined;
    const msg = std.mem.print(&buf, "eessi-arch-guard: skipped, {s}: {s}", .{ kind, detail }) catch "eessi-arch-guard: skipped";
    // No allocation here: a crash may have come from the allocator.
    var clean: [1024]u8 = undefined;
    var n: usize = 0;
    for (msg) |ch| {
        clean[n] = if (ch < 32 or ch == 127) ' ' else ch;
        n += 1;
    }
    if (emergency_shell) {
        cli.put(2, clean[0..n]);
        cli.put(2, "\n");
    } else if (emergency_announce) {
        cli.put(1, "print ");
        cli.put(1, clean[0..n]);
        cli.put(1, "\n");
    }
}

/// _announce for a prolog: SLURM_LOCALID unset or exactly "0". Read from the
/// raw block, without allocating; the first entry of a name wins, as in
/// envmap.fromEnvp.
fn localTaskZero(envp: proc.Envp) bool {
    const prefix = "SLURM_LOCALID=";
    var i: usize = 0;
    while (envp[i]) |p| : (i += 1) {
        const entry = std.mem.span(p);
        if (std.mem.startsWith(u8, entry, prefix)) return std.mem.eql(u8, entry[prefix.len..], "0");
    }
    return true;
}

/// Output a chained prolog gave and that is not yet written: Python's main
/// writes it before the skip line, so an emergency does too.
var emergency_chain: []const u8 = "";

/// Python's main on an uncaught exception: stop any command the decision
/// started, write the chained output, then the skip line, and exit 0 (or
/// the strict status).
fn emergencyExit(kind: []const u8, detail: []const u8) noreturn {
    guard.stopChild();
    if (emergency_chain.len > 0) writeStdout(emergency_chain, "");
    emergencyWrite(kind, detail);
    linux.exit_group(if (emergency_strict) strict_failure_exit else 0);
}

/// Out of memory, as a MemoryError reaching Python's main.
pub fn oom() noreturn {
    emergencyExit("MemoryError", "out of memory");
}

/// A panic is an uncaught exception to the Python guard: the skip line
/// "eessi-arch-guard: skipped, panic: MESSAGE", then exit 0, or 78 with
/// --strict, so the task still starts.
pub const panic = std.debug.FullPanic(struct {
    fn f(msg: []const u8, _: ?usize) noreturn {
        emergencyExit("panic", msg);
    }
}.f);

fn onCrash(sig: linux.SIG) callconv(.c) void {
    emergencyExit("signal", switch (sig) {
        .SEGV => "SIGSEGV",
        .BUS => "SIGBUS",
        .ILL => "SIGILL",
        .FPE => "SIGFPE",
        else => "signal",
    });
}

/// The stack the crash handlers run on: a SIGSEGV from a stack overflow
/// has no stack left to run a handler on the thread's own.
var crash_stack: [64 * 1024]u8 align(16) = undefined;

fn installCrashHandlers() void {
    const alt: linux.stack_t = .{ .sp = &crash_stack, .flags = 0, .size = crash_stack.len };
    const on_stack = linux.errno(linux.sigaltstack(&alt, null)) == .SUCCESS;
    var act: linux.Sigaction = .{ .handler = .{ .handler = onCrash }, .mask = linux.sigemptyset(), .flags = if (on_stack) linux.SA.ONSTACK else 0 };
    for ([_]linux.SIG{ .SEGV, .BUS, .ILL, .FPE }) |s| _ = linux.sigaction(s, &act, null);
    // A closed stdout must not kill the prolog: writes fail instead.
    var ign: linux.Sigaction = .{ .handler = .{ .handler = linux.SIG.IGN }, .mask = linux.sigemptyset(), .flags = 0 };
    _ = linux.sigaction(.PIPE, &ign, null);
}

// ---- arguments ---------------------------------------------------------

const Args = struct {
    shell: bool = false,
    restore: bool = false,
    help: bool = false,
    version: bool = false,
    strict: bool = false,
    dry_run: bool = false,
    json: bool = false,
    chain: ?[]const u8 = null,
    timeout: ?[]const u8 = null,
    syslog: ?[]const u8 = null,
    check_submit: bool = false,
    partition: ?[]const u8 = null,
    constraint: ?[]const u8 = null,
    eb_stack: ?[]const u8 = null,
};

/// _option_value: the word after argv[i.*], with i.* moved onto it, unless
/// it starts with "-" and so is an option of its own; "" then, and when
/// there is no word after it.
fn optionValue(argv: []const []const u8, i: *usize) []const u8 {
    if (i.* + 1 >= argv.len or std.mem.startsWith(u8, argv[i.* + 1], "-")) return "";
    i.* += 1;
    return argv[i.*];
}

/// parse_args: documented flags only; anything else is skipped.
fn parseArgs(argv: []const []const u8) Args {
    var a: Args = .{};
    var i: usize = 0;
    while (i < argv.len) : (i += 1) {
        const arg = argv[i];
        if (std.mem.eql(u8, arg, "--shell")) {
            a.shell = true;
        } else if (std.mem.eql(u8, arg, "--strict")) {
            a.strict = true;
        } else if (std.mem.eql(u8, arg, "--dry-run")) {
            a.dry_run = true;
        } else if (std.mem.eql(u8, arg, "--restore")) {
            a.restore = true;
        } else if (std.mem.eql(u8, arg, "--json")) {
            a.json = true;
        } else if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            a.help = true;
        } else if (std.mem.eql(u8, arg, "--version")) {
            a.version = true;
        } else if (std.mem.eql(u8, arg, "--chain")) {
            a.chain = optionValue(argv, &i);
        } else if (std.mem.startsWith(u8, arg, "--chain=")) {
            a.chain = arg["--chain=".len..];
        } else if (std.mem.eql(u8, arg, "--timeout")) {
            a.timeout = optionValue(argv, &i);
        } else if (std.mem.startsWith(u8, arg, "--timeout=")) {
            a.timeout = arg["--timeout=".len..];
        } else if (std.mem.eql(u8, arg, "--syslog")) {
            a.syslog = optionValue(argv, &i);
        } else if (std.mem.startsWith(u8, arg, "--syslog=")) {
            a.syslog = arg["--syslog=".len..];
        } else if (std.mem.eql(u8, arg, "--check-submit")) {
            a.check_submit = true;
        } else if (std.mem.eql(u8, arg, "--partition")) {
            a.partition = optionValue(argv, &i);
        } else if (std.mem.startsWith(u8, arg, "--partition=")) {
            a.partition = arg["--partition=".len..];
        } else if (std.mem.eql(u8, arg, "--constraint")) {
            a.constraint = optionValue(argv, &i);
        } else if (std.mem.startsWith(u8, arg, "--constraint=")) {
            a.constraint = arg["--constraint=".len..];
        } else if (std.mem.eql(u8, arg, "--eb-stack")) {
            a.eb_stack = optionValue(argv, &i);
        } else if (std.mem.startsWith(u8, arg, "--eb-stack=")) {
            a.eb_stack = arg["--eb-stack=".len..];
        }
    }
    return a;
}

fn contains(argv: []const []const u8, word: []const u8) bool {
    for (argv) |a| if (std.mem.eql(u8, a, word)) return true;
    return false;
}

/// float(text) as Python parses it: surrounding whitespace, a sign, digits
/// with single underscores between them, a fraction, an exponent, or
/// inf/infinity/nan in any case.
fn pyFloat(arena: Allocator, raw: []const u8) ?f64 {
    const s = text.pyStrip(raw);
    if (s.len == 0) return null;
    var body = s;
    var neg = false;
    if (body[0] == '+' or body[0] == '-') {
        neg = body[0] == '-';
        body = body[1..];
    }
    if (std.ascii.eqlIgnoreCase(body, "inf") or std.ascii.eqlIgnoreCase(body, "infinity"))
        return if (neg) -std.math.inf(f64) else std.math.inf(f64);
    if (std.ascii.eqlIgnoreCase(body, "nan")) return std.math.nan(f64);
    var clean: std.ArrayList(u8) = .empty;
    var i: usize = 0;
    var digits_before = false;
    var digits_after = false;
    var seen_digit_in_run = false;
    while (i < body.len and (std.ascii.isDigit(body[i]) or body[i] == '_')) : (i += 1) {
        if (body[i] == '_') {
            if (!seen_digit_in_run or i + 1 >= body.len or !std.ascii.isDigit(body[i + 1])) return null;
            continue;
        }
        seen_digit_in_run = true;
        digits_before = true;
        clean.append(arena, body[i]) catch return null;
    }
    if (i < body.len and body[i] == '.') {
        clean.append(arena, '.') catch return null;
        i += 1;
        seen_digit_in_run = false;
        while (i < body.len and (std.ascii.isDigit(body[i]) or body[i] == '_')) : (i += 1) {
            if (body[i] == '_') {
                if (!seen_digit_in_run or i + 1 >= body.len or !std.ascii.isDigit(body[i + 1])) return null;
                continue;
            }
            seen_digit_in_run = true;
            digits_after = true;
            clean.append(arena, body[i]) catch return null;
        }
    }
    if (!digits_before and !digits_after) return null;
    if (i < body.len and (body[i] == 'e' or body[i] == 'E')) {
        clean.append(arena, 'e') catch return null;
        i += 1;
        if (i < body.len and (body[i] == '+' or body[i] == '-')) {
            clean.append(arena, body[i]) catch return null;
            i += 1;
        }
        seen_digit_in_run = false;
        var exp_digits = false;
        while (i < body.len and (std.ascii.isDigit(body[i]) or body[i] == '_')) : (i += 1) {
            if (body[i] == '_') {
                if (!seen_digit_in_run or i + 1 >= body.len or !std.ascii.isDigit(body[i + 1])) return null;
                continue;
            }
            seen_digit_in_run = true;
            exp_digits = true;
            clean.append(arena, body[i]) catch return null;
        }
        if (!exp_digits) return null;
    }
    if (i != body.len) return null;
    if (clean.items[0] == '.') clean.insert(arena, 0, '0') catch return null;
    const v = std.fmt.parseFloat(f64, clean.items) catch return null;
    return if (neg) -v else v;
}

/// parse_timeout: a positive number of seconds at most TIMEOUT_MAX.
fn parseTimeout(arena: Allocator, s: []const u8) ?f64 {
    const v = pyFloat(arena, s) orelse return null;
    if (std.math.isNan(v) or v <= 0 or v > timeout_max) return null;
    return v;
}

// ---- the site configuration --------------------------------------------

const Settings = std.StringArrayHashMapUnmanaged([]const u8);

const Config = struct {
    path: ?[]const u8 = null,
    settings: ?Settings = null,
    warning: ?[]const u8 = null,
};

fn pyStripBytes(s: []const u8) []const u8 {
    return std.mem.trim(u8, s, " \t\n\r\x0b\x0c");
}

/// parse_config_bytes: KEY = VALUE lines; '#' comments; a key that is not
/// UTF-8 is skipped; a value that is not UTF-8 is kept as its bytes and
/// named in the returned list.
fn parseConfigBytes(arena: Allocator, blob: []const u8, bad: *std.ArrayList([]const u8)) Allocator.Error!Settings {
    var settings: Settings = .empty;
    var i: usize = 0;
    while (i < blob.len) {
        var j = i;
        while (j < blob.len and blob[j] != '\n' and blob[j] != '\r') j += 1;
        const raw = blob[i..j];
        // bytes.splitlines: \r\n, \r and \n each end a line.
        i = if (j + 1 < blob.len and blob[j] == '\r' and blob[j + 1] == '\n') j + 2 else j + 1;
        const line = pyStripBytes(raw);
        if (line.len == 0 or line[0] == '#') continue;
        const eq = std.mem.findScalar(u8, line, '=') orelse continue;
        const key = pyStripBytes(line[0..eq]);
        const value = pyStripBytes(line[eq + 1 ..]);
        if (!std.unicode.utf8ValidateSlice(key) or key.len == 0) continue;
        if (!std.unicode.utf8ValidateSlice(value)) try bad.append(arena, key);
        try settings.put(arena, key, value);
    }
    return settings;
}

const errno_text = [_]struct { e: linux.E, s: []const u8 }{
    .{ .e = .NOENT, .s = "No such file or directory" },
    .{ .e = .ACCES, .s = "Permission denied" },
    .{ .e = .PERM, .s = "Operation not permitted" },
    .{ .e = .LOOP, .s = "Too many levels of symbolic links" },
    .{ .e = .NOTDIR, .s = "Not a directory" },
    .{ .e = .IO, .s = "Input/output error" },
    .{ .e = .ISDIR, .s = "Is a directory" },
};

fn osErrorText(arena: Allocator, e: linux.E, path: []const u8) []const u8 {
    for (errno_text) |t| if (t.e == e)
        return arena.print("[Errno {d}] {s}: '{s}'", .{ @backingInt(e), t.s, path }) catch t.s;
    return arena.print("[Errno {d}] Unknown error {d}: '{s}'", .{ @backingInt(e), @backingInt(e), path }) catch "error";
}

fn lstatRaw(path: []const u8, x: *linux.Statx) linux.E {
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, path) catch return .NAMETOOLONG;
    const mask: linux.STATX = .{ .TYPE = true, .MODE = true, .UID = true };
    return linux.errno(linux.statx(linux.AT.FDCWD, z.ptr, linux.AT.SYMLINK_NOFOLLOW, mask, x));
}

/// _config_untrusted: the reason a config path is not trusted, or null.
fn configUntrusted(arena: Allocator, path: []const u8) ?[]const u8 {
    var x: linux.Statx = undefined;
    const e = lstatRaw(path, &x);
    if (e != .SUCCESS) return arena.print("{s} ({s})", .{ path, osErrorText(arena, e, path) }) catch path;
    const fmt_bits = @as(u32, x.mode) & fsx.S.IFMT;
    if (fmt_bits == fsx.S.IFLNK) return arena.print("{s} is a symlink and is ignored", .{path}) catch path;
    if (fmt_bits != fsx.S.IFREG) return arena.print("{s} is not a regular file and is ignored", .{path}) catch path;
    if (x.uid != 0) return arena.print("{s} is not owned by root and is ignored", .{path}) catch path;
    if (x.mode & 0o022 != 0) return arena.print("{s} is writable by group or others and is ignored", .{path}) catch path;
    return null;
}

fn exists(path: []const u8) bool {
    _ = fsx.statAt(fsx.cwd, path, true) catch return false;
    return true;
}

/// site_config: /etc/eessi-arch-guard.conf, else the file beside the
/// program as it was invoked; the first that exists decides.
fn siteConfig(arena: Allocator, invoked: []const u8) Config {
    const beside = configBeside(arena, invoked) catch oom();
    for ([_][]const u8{ "/etc/" ++ config_name, beside }) |path| {
        if (!exists(path)) continue;
        if (configUntrusted(arena, path)) |reason|
            return .{ .path = path, .warning = arena.print("eessi-arch-guard: config ignored: {s}", .{reason}) catch null };
        // Checked again on the open file, without following a link.
        const f = fsx.openRegular(fsx.cwd, path, false) catch |err|
            return .{ .path = path, .warning = arena.print("eessi-arch-guard: config ignored: {s}: {t}", .{ path, err }) catch null };
        defer sys.close(f.fd);
        if (f.stat.uid != 0 or f.stat.mode & 0o022 != 0)
            return .{ .path = path, .warning = arena.print("eessi-arch-guard: config ignored: {s} changed while it was read", .{path}) catch null };
        const blob = fsx.readAll(arena, f.fd, 1 << 20) catch |err|
            return .{ .path = path, .warning = arena.print("eessi-arch-guard: config ignored: {s}: {t}", .{ path, err }) catch null };
        var bad: std.ArrayList([]const u8) = .empty;
        const settings = parseConfigBytes(arena, blob, &bad) catch return .{ .path = path };
        if (bad.items.len > 0) {
            const shown = std.mem.join(arena, " ", bad.items) catch "";
            return .{ .path = path, .settings = settings, .warning = arena.print("eessi-arch-guard: config value kept with its original bytes: {s}", .{shown}) catch null };
        }
        return .{ .path = path, .settings = settings };
    }
    return .{};
}

fn chainPathOk(path: []const u8) bool {
    if (path.len == 0 or path[0] != '/' or text.hasLineBreakOrNul(path)) return false;
    return guard.lmodInvocable(path);
}

fn resolveTimeout(arena: Allocator, cli_text: ?[]const u8, settings: ?Settings) struct { value: f64, warning: ?[]const u8 } {
    var chosen = default_timeout;
    var warnings: std.ArrayList([]const u8) = .empty;
    if (settings) |s| if (s.get("timeout")) |t| {
        if (parseTimeout(arena, t)) |v| chosen = v else warnings.append(arena, "eessi-arch-guard: config timeout is not a positive number of seconds at most " ++ timeout_max_text) catch oom();
    };
    if (cli_text) |t| {
        if (parseTimeout(arena, t)) |v| chosen = v else warnings.append(arena, "eessi-arch-guard: --timeout is not a positive number of seconds at most " ++ timeout_max_text) catch oom();
    }
    if (warnings.items.len == 0) return .{ .value = chosen, .warning = null };
    return .{ .value = chosen, .warning = std.mem.join(arena, " ", warnings.items) catch null };
}

fn writeMessage(arena: Allocator, fd: sys.fd_t, msg: []const u8) void {
    const b = text.messageBytes(arena, msg) catch return;
    cli.put(fd, b);
    cli.put(fd, "\n");
}

fn writeStdout(chain_out: []const u8, body: []const u8) void {
    emergency_chain = "";
    if (chain_out.len > 0) {
        cli.put(1, chain_out);
        if (chain_out[chain_out.len - 1] != '\n') cli.put(1, "\n");
    }
    cli.put(1, body);
}

/// The path this program was started by, for the config beside it: the
/// path execve was given, which the kernel passes as AT_EXECFN and gives a
/// script's interpreter, where the Python guard's __file__ comes from. A
/// PATH search makes it the link the search found, wherever that points.
/// Without AT_EXECFN, argv[0] when it names a path, else /proc/self/exe.
fn invokedPath(arena: Allocator, argv0: []const u8) []const u8 {
    const execfn = linux.getauxval(std.elf.AT.EXECFN);
    if (execfn != 0) return std.mem.span(@as([*:0]const u8, @ptrFromInt(execfn)));
    if (std.mem.findScalar(u8, argv0, '/') != null) return argv0;
    return selfExe(arena);
}

/// posixpath.normpath: "." parts and repeated slashes dropped, and each
/// ".." taking off the part before it, without following symbolic links.
/// One or two leading slashes stay; three or more become one.
fn normPath(gpa: Allocator, path: []const u8) Allocator.Error![]u8 {
    const slashes: usize = if (!std.mem.startsWith(u8, path, "/"))
        0
    else if (std.mem.startsWith(u8, path, "//") and !std.mem.startsWith(u8, path, "///"))
        2
    else
        1;
    var parts: std.ArrayList([]const u8) = .empty;
    defer parts.deinit(gpa);
    var it = std.mem.splitScalar(u8, path, '/');
    while (it.next()) |part| {
        if (part.len == 0 or std.mem.eql(u8, part, ".")) continue;
        const after_up = parts.items.len > 0 and std.mem.eql(u8, parts.items[parts.items.len - 1], "..");
        if (!std.mem.eql(u8, part, "..") or (slashes == 0 and parts.items.len == 0) or after_up) {
            try parts.append(gpa, part);
        } else {
            _ = parts.pop();
        }
    }
    var out: std.ArrayList(u8) = .empty;
    errdefer out.deinit(gpa);
    try out.appendNTimes(gpa, '/', slashes);
    for (parts.items, 0..) |part, i| {
        if (i > 0) try out.append(gpa, '/');
        try out.appendSlice(gpa, part);
    }
    if (out.items.len == 0) try out.append(gpa, '.');
    return out.toOwnedSlice(gpa);
}

/// os.path.abspath of the Python guard's __file__: Python's start-up puts
/// a relative script path after the working directory and a "/", even when
/// that directory is "/" itself, and abspath normalizes the result.
fn absPath(gpa: Allocator, path: []const u8) Allocator.Error![]u8 {
    if (std.mem.startsWith(u8, path, "/")) return normPath(gpa, path);
    var buf: [sys.path_max]u8 = undefined;
    const cwd = fsx.realpath(fsx.cwd, ".", &buf) catch return normPath(gpa, path);
    const joined = try std.mem.concat(gpa, u8, &.{ cwd, "/", path });
    defer gpa.free(joined);
    return normPath(gpa, joined);
}

const config_name = "eessi-arch-guard.conf";

/// site_config's second candidate, os.path.join(os.path.dirname(
/// os.path.abspath(invoked)), "eessi-arch-guard.conf").
fn configBeside(gpa: Allocator, invoked: []const u8) Allocator.Error![]u8 {
    const abs = try absPath(gpa, invoked);
    defer gpa.free(abs);
    const cut = std.mem.findScalarLast(u8, abs, '/') orelse return gpa.dupe(u8, config_name);
    var head: []const u8 = abs[0 .. cut + 1];
    if (std.mem.findNone(u8, head, "/") != null) head = std.mem.trimEnd(u8, head, "/");
    return std.mem.concat(gpa, u8, &.{ head, if (std.mem.endsWith(u8, head, "/")) "" else "/", config_name });
}

fn selfExe(arena: Allocator) []const u8 {
    var buf: [sys.path_max]u8 = undefined;
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, "/proc/self/exe", &buf, buf.len }) catch return "/proc/self/exe";
    return arena.dupe(u8, buf[0..n]) catch "/proc/self/exe";
}

const DryRunReport = struct {
    mode: []const u8,
    strict: bool,
    announce: bool,
    config_path: ?[]const u8,
    config_status: []const u8,
    config_warning: ?[]const u8,
    chain_path: ?[]const u8,
    chain_source: []const u8,
    chain_status: []const u8,
    timeout: f64,
    syslog_text: ?[]const u8,
    syslog_all: bool,
    syslog_pen: []const u8,
    job_id: []const u8,
    step_id: []const u8,
    local_id: ?[]const u8,
    cache_path: ?[]const u8,
    cache_hit: bool,
    cached_kind: ?guard.Kind,
    decision: guard.DryRunDecision,
    exit_code: u8,
};

fn writeJsonOptStr(out: *std.ArrayList(u8), arena: Allocator, value: ?[]const u8) Allocator.Error!void {
    if (value) |s| {
        try json.writeCacheString(out, arena, s);
    } else {
        try out.appendSlice(arena, "null");
    }
}

fn writeJsonBool(out: *std.ArrayList(u8), arena: Allocator, value: bool) Allocator.Error!void {
    try out.appendSlice(arena, if (value) "true" else "false");
}

fn formatTimeoutText(arena: Allocator, t: f64) Allocator.Error![]const u8 {
    if (!std.math.isNan(t) and !std.math.isInf(t) and @floor(t) == t and t >= 0.0 and t <= 2147483647.0) {
        return arena.print("{d}", .{@as(i64, @intFromFloat(t))});
    }
    return json.dumps(arena, .{ .float = t }, 0, false);
}

fn nonEmptySlice(s: []const u8) ?[]const u8 {
    return if (s.len > 0) s else null;
}

fn dryRunCause(d: guard.DryRunDecision) ?[]const u8 {
    if (d.kind == .skip) return jsonout.skipCause(d.detail);
    if (d.kind == .failed and d.marker.len > 0) {
        if (std.mem.find(u8, d.marker, "->")) |at| return d.marker[at + 2 ..];
    }
    return null;
}

fn dryRunJsonDoc(arena: Allocator, r: DryRunReport) Allocator.Error!Py {
    const d = r.decision;
    const decision_str = @tagName(d.kind);

    var config: std.ArrayList(Py.Entry) = .empty;
    try config.append(arena, .{ .key = "path", .value = Py.opt(r.config_path) });
    try config.append(arena, .{ .key = "status", .value = Py.s(r.config_status) });
    try config.append(arena, .{ .key = "warning", .value = Py.opt(r.config_warning) });

    var chain: std.ArrayList(Py.Entry) = .empty;
    try chain.append(arena, .{ .key = "path", .value = Py.opt(r.chain_path) });
    try chain.append(arena, .{ .key = "source", .value = Py.s(r.chain_source) });
    try chain.append(arena, .{ .key = "status", .value = Py.s(r.chain_status) });

    var syslog_e: std.ArrayList(Py.Entry) = .empty;
    try syslog_e.append(arena, .{ .key = "target", .value = Py.opt(r.syslog_text) });
    try syslog_e.append(arena, .{ .key = "all", .value = .{ .bool = r.syslog_all } });
    try syslog_e.append(arena, .{ .key = "pen", .value = Py.s(r.syslog_pen) });

    var job: std.ArrayList(Py.Entry) = .empty;
    try job.append(arena, .{ .key = "id", .value = Py.opt(nonEmptySlice(r.job_id)) });
    try job.append(arena, .{
        .key = "step",
        .value = Py.opt(if (r.job_id.len > 0) r.step_id else null),
    });
    try job.append(arena, .{ .key = "localid", .value = Py.opt(r.local_id) });

    var cache: std.ArrayList(Py.Entry) = .empty;
    try cache.append(arena, .{ .key = "path", .value = Py.opt(r.cache_path) });
    try cache.append(arena, .{ .key = "hit", .value = .{ .bool = r.cache_hit } });
    try cache.append(arena, .{
        .key = "cached_kind",
        .value = Py.opt(if (r.cached_kind) |k| @tagName(k) else null),
    });

    var modules_list: std.ArrayList(Py) = .empty;
    for (d.modules) |m| try modules_list.append(arena, Py.s(m));

    var top: std.ArrayList(Py.Entry) = .empty;
    try top.append(arena, .{ .key = "announce", .value = .{ .bool = r.announce } });
    try top.append(arena, .{ .key = "cache", .value = .{ .dict = cache.items } });
    try top.append(arena, .{ .key = "cached", .value = .{ .bool = r.cache_hit } });
    try top.append(arena, .{ .key = "cause", .value = Py.opt(dryRunCause(d)) });
    try top.append(arena, .{ .key = "chain", .value = .{ .dict = chain.items } });
    try top.append(arena, .{ .key = "compare_accel", .value = .{ .bool = d.compare_accel } });
    try top.append(arena, .{ .key = "config", .value = .{ .dict = config.items } });
    try top.append(arena, .{ .key = "decision", .value = Py.s(decision_str) });
    try top.append(arena, .{ .key = "detail", .value = Py.opt(d.detail) });
    try top.append(arena, .{ .key = "eessi", .value = Py.opt(nonEmptySlice(d.eessi)) });
    try top.append(arena, .{ .key = "exit", .value = .{ .int = r.exit_code } });
    try top.append(arena, .{ .key = "exit_semantic", .value = Py.s(jsonout.exitSemantic(r.exit_code)) });
    try top.append(arena, .{ .key = "job", .value = .{ .dict = job.items } });
    try top.append(arena, .{ .key = "kind", .value = Py.s("decision") });
    try top.append(arena, .{ .key = "marker", .value = Py.opt(nonEmptySlice(d.marker)) });
    try top.append(arena, .{ .key = "message", .value = Py.opt(d.message) });
    try top.append(arena, .{ .key = "mode", .value = Py.s(r.mode) });
    try top.append(arena, .{ .key = "modules", .value = .{ .list = modules_list.items } });
    try top.append(arena, .{ .key = "now", .value = Py.opt(nonEmptySlice(d.now_cpu)) });
    try top.append(arena, .{ .key = "now_accel", .value = Py.opt(nonEmptySlice(d.now_accel)) });
    try top.append(arena, .{ .key = "pin", .value = Py.opt(nonEmptySlice(d.pin)) });
    try top.append(arena, .{ .key = "reload_needed", .value = .{ .bool = jsonout.reloadNeeded(d.kind) } });
    try top.append(arena, .{ .key = "schema_version", .value = .{ .int = jsonout.schema_version } });
    try top.append(arena, .{ .key = "shell", .value = .{ .bool = std.mem.eql(u8, r.mode, "shell") } });
    try top.append(arena, .{ .key = "strict", .value = .{ .bool = r.strict } });
    try top.append(arena, .{ .key = "syslog", .value = .{ .dict = syslog_e.items } });
    try top.append(arena, .{ .key = "timeout", .value = .{ .float = r.timeout } });
    try top.append(arena, .{ .key = "was", .value = Py.opt(nonEmptySlice(d.was_cpu)) });
    try top.append(arena, .{ .key = "was_accel", .value = Py.opt(nonEmptySlice(d.was_accel)) });
    return .{ .dict = top.items };
}

fn compactJsonLine(arena: Allocator, pretty: []const u8) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    for (pretty) |ch| {
        if (ch != '\n') try out.append(arena, ch) else if (out.items.len > 0 and out.items[out.items.len - 1] == ' ') {
            _ = out.pop();
        }
    }
    return out.items;
}

/// Match Python's ``json.dumps`` comma spacing outside quoted strings.
fn pythonJsonCommas(arena: Allocator, s: []const u8) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    var in_str = false;
    var esc = false;
    for (s) |ch| {
        if (in_str) {
            try out.append(arena, ch);
            if (esc) esc = false else if (ch == '\\') esc = true else if (ch == '"') in_str = false;
            continue;
        }
        if (ch == '"') {
            in_str = true;
            try out.append(arena, ch);
            continue;
        }
        if (ch == ',') {
            try out.appendSlice(arena, ", ");
            continue;
        }
        try out.append(arena, ch);
    }
    return out.items;
}

fn renderDryRunJson(arena: Allocator, r: DryRunReport) Allocator.Error![]u8 {
    const doc = try dryRunJsonDoc(arena, r);
    const pretty = try json.dumps(arena, doc, 0, true);
    const compact = try compactJsonLine(arena, pretty);
    const line = try pythonJsonCommas(arena, compact);
    return try std.fmt.allocPrint(arena, "{s}\n", .{line});
}

fn oneLineField(arena: Allocator, s: []const u8) Allocator.Error![]const u8 {
    return text.oneLine(arena, s, 500);
}

fn renderDryRunText(arena: Allocator, r: DryRunReport) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    const d = r.decision;
    const config_line = if (std.mem.eql(u8, r.config_status, "none"))
        "none"
    else
        try arena.print("{s} ({s})", .{ if (r.config_path) |p| try oneLineField(arena, p) else "none", r.config_status });

    const chain_line = if (std.mem.eql(u8, r.chain_status, "none"))
        "none"
    else if (std.mem.eql(u8, r.chain_status, "skipped"))
        "none (skipped in --shell)"
    else
        try arena.print("{s} ({s}, {s})", .{
            if (r.chain_path) |p| (if (p.len > 0) try oneLineField(arena, p) else "(empty)") else "(empty)",
            r.chain_source,
            r.chain_status,
        });

    const step_line = if (r.job_id.len == 0)
        "none"
    else if (r.step_id.len > 0)
        try oneLineField(arena, r.step_id)
    else
        "(empty)";

    const localid_line = if (r.local_id) |lid|
        (if (lid.len > 0) try oneLineField(arena, lid) else "(empty)")
    else
        "unset";

    const cache_line = if (r.cache_path) |cp|
        (if (r.cache_hit)
            try arena.print("{s} (hit: {s})", .{ try oneLineField(arena, cp), if (r.cached_kind) |k| @tagName(k) else "unknown" })
        else
            try arena.print("{s} (miss)", .{try oneLineField(arena, cp)}))
    else
        "none";

    var mods_buf: std.ArrayList(u8) = .empty;
    for (d.modules, 0..) |m, idx| {
        if (idx > 0) try mods_buf.append(arena, ' ');
        try mods_buf.appendSlice(arena, try oneLineField(arena, m));
    }

    try out.print(arena, "mode: {s}\n", .{r.mode});
    try out.print(arena, "strict: {s}\n", .{if (r.strict) "true" else "false"});
    try out.print(arena, "announce: {s}\n", .{if (r.announce) "true" else "false"});
    try out.print(arena, "config: {s}\n", .{config_line});
    try out.print(arena, "chain: {s}\n", .{chain_line});
    try out.print(arena, "timeout: {s}\n", .{try formatTimeoutText(arena, r.timeout)});
    try out.print(arena, "syslog: {s}\n", .{if (r.syslog_text) |s| try oneLineField(arena, s) else "none"});
    try out.print(arena, "syslog_all: {s}\n", .{if (r.syslog_all) "true" else "false"});
    try out.print(arena, "syslog_pen: {s}\n", .{r.syslog_pen});
    try out.print(arena, "job: {s}\n", .{if (r.job_id.len > 0) try oneLineField(arena, r.job_id) else "none"});
    try out.print(arena, "step: {s}\n", .{step_line});
    try out.print(arena, "localid: {s}\n", .{localid_line});
    try out.print(arena, "cache: {s}\n", .{cache_line});
    try out.print(arena, "decision: {s}\n", .{@tagName(d.kind)});
    try out.print(arena, "was: {s}\n", .{if (d.was_cpu.len > 0) try oneLineField(arena, d.was_cpu) else "none"});
    try out.print(arena, "now: {s}\n", .{if (d.now_cpu.len > 0) try oneLineField(arena, d.now_cpu) else "none"});
    try out.print(arena, "was_accel: {s}\n", .{if (d.was_accel.len > 0) try oneLineField(arena, d.was_accel) else "none"});
    try out.print(arena, "now_accel: {s}\n", .{if (d.now_accel.len > 0) try oneLineField(arena, d.now_accel) else "none"});
    try out.print(arena, "compare_accel: {s}\n", .{if (d.compare_accel) "true" else "false"});
    try out.print(arena, "pin: {s}\n", .{if (d.pin.len > 0) try oneLineField(arena, d.pin) else "none"});
    try out.print(arena, "eessi: {s}\n", .{if (d.eessi.len > 0) try oneLineField(arena, d.eessi) else "none"});
    try out.print(arena, "modules: {s}\n", .{if (d.modules.len > 0) mods_buf.items else "none"});
    try out.print(arena, "marker: {s}\n", .{if (d.marker.len > 0) try oneLineField(arena, d.marker) else "none"});
    try out.print(arena, "detail: {s}\n", .{if (d.detail) |dt| try oneLineField(arena, dt) else "none"});
    try out.print(arena, "message: {s}\n", .{if (d.message) |msg| try oneLineField(arena, msg) else "none"});
    try out.print(arena, "exit: {d}\n", .{r.exit_code});
    return out.items;
}

fn submitSelector(s: []const u8) bool {
    if (s.len == 0 or s.len > 64) return false;
    for (s) |ch| {
        if (!std.ascii.isAlphanumeric(ch) and ch != '_' and ch != '.' and ch != '+' and ch != '-') return false;
    }
    return true;
}

fn submitTarget(arena: Allocator, settings: ?Settings, partition: []const u8, constraint: []const u8, why: *[]const u8) ?[]const u8 {
    if (!submitSelector(partition)) {
        why.* = "--partition needs one safe partition name";
        return null;
    }
    if (constraint.len > 0 and !submitSelector(constraint)) {
        why.* = "--constraint needs one safe constraint name";
        return null;
    }
    const s = settings orelse {
        why.* = "site config has no submit target mappings";
        return null;
    };
    const part_key = arena.print("submit_target.partition.{s}", .{partition}) catch oom();
    const target = s.get(part_key) orelse {
        why.* = arena.print("site config has no {s} mapping", .{part_key}) catch part_key;
        return null;
    };
    if (!text.safeRelSubdir(target)) {
        why.* = arena.print("{s} is not a safe relative target", .{part_key}) catch part_key;
        return null;
    }
    if (constraint.len > 0) {
        const constraint_key = arena.print("submit_target.constraint.{s}", .{constraint}) catch oom();
        const other = s.get(constraint_key) orelse {
            why.* = arena.print("site config has no {s} mapping", .{constraint_key}) catch constraint_key;
            return null;
        };
        if (!text.safeRelSubdir(other)) {
            why.* = arena.print("{s} is not a safe relative target", .{constraint_key}) catch constraint_key;
            return null;
        }
        if (!std.mem.eql(u8, target, other)) {
            why.* = arena.print("{s} and {s} select different targets", .{ part_key, constraint_key }) catch "submit selectors select different targets";
            return null;
        }
    }
    return target;
}

fn findExecutable(arena: Allocator, env: envmap.Map, selected: []const u8) ?[]const u8 {
    if (std.mem.findScalar(u8, selected, '/') != null)
        return if (guard.lmodInvocable(selected)) selected else null;
    var paths = std.mem.splitScalar(u8, env.get("PATH") orelse "", ':');
    while (paths.next()) |dir| {
        if (dir.len == 0) continue;
        const candidate = std.fs.path.join(arena, &.{ dir, selected }) catch return null;
        if (guard.lmodInvocable(candidate)) return candidate;
    }
    return null;
}

fn submitWorkDir(arena: Allocator) ?[]const u8 {
    var tries: usize = 0;
    while (tries < 100) : (tries += 1) {
        var random: [8]u8 = undefined;
        _ = sys.call(linux.getrandom, .{ &random, random.len, 0 }) catch return null;
        const name = std.fmt.bytesToHex(random, .lower);
        const path = arena.print("/tmp/eessi-arch-guard-submit-{s}", .{name}) catch return null;
        var buf: [sys.path_max:0]u8 = undefined;
        const z = sys.z(&buf, path) catch return null;
        switch (linux.errno(linux.mkdirat(linux.AT.FDCWD, z.ptr, 0o700))) {
            .SUCCESS => return path,
            .EXIST => continue,
            else => return null,
        }
    }
    return null;
}

const SubmitModule = struct {
    name: []const u8,
    file: []const u8,
};

fn plannerModule(arena: Allocator, module: []const u8) []const u8 {
    const hex = "0123456789abcdef";
    var out: std.ArrayList(u8) = .empty;
    out.appendSlice(arena, "EAGCheck") catch oom();
    for (module) |ch| {
        out.append(arena, hex[ch >> 4]) catch oom();
        out.append(arena, hex[ch & 15]) catch oom();
    }
    out.appendSlice(arena, "/1.0") catch oom();
    return out.items;
}

fn easyconfigFilename(arena: Allocator, module: []const u8) []const u8 {
    const identity = plannerModule(arena, module);
    return arena.print("{s}-1.0.eb", .{identity[0 .. identity.len - "/1.0".len]}) catch oom();
}

fn loadedSubmitModules(arena: Allocator, env: envmap.Map, why: *[]const u8) ?[]SubmitModule {
    const raw = env.get("LOADEDMODULES") orelse "";
    var found: std.ArrayList(SubmitModule) = .empty;
    var any = false;
    var parts = std.mem.splitScalar(u8, raw, ':');
    while (parts.next()) |name| {
        if (name.len == 0) continue;
        any = true;
        if (!text.moduleName(name)) {
            why.* = "LOADEDMODULES contains an unsafe module name";
            return null;
        }
        if (std.mem.startsWith(u8, name, "EESSI/") and std.mem.findScalar(u8, name["EESSI/".len..], '/') == null) continue;
        found.append(arena, .{ .name = name, .file = easyconfigFilename(arena, name) }) catch oom();
    }
    if (!any) {
        why.* = "LOADEDMODULES is empty";
        return null;
    }
    return found.items;
}

fn walkInventory(arena: Allocator, dir: sys.fd_t, prefix: []const u8, found: *std.ArrayList([]const u8)) void {
    var it = fsx.DirIter.init(arena, dir) catch return;
    defer it.deinit(arena);
    while (it.next() catch null) |entry| {
        const kind = fsx.entryType(dir, entry) catch continue;
        const relative = if (prefix.len > 0)
            std.fs.path.join(arena, &.{ prefix, entry.name }) catch oom()
        else
            arena.dupe(u8, entry.name) catch oom();
        if (kind == fsx.DT.DIR) {
            if (entry.name.len == 0 or entry.name[0] == '.') continue;
            const child = fsx.openDir(dir, entry.name, false) catch continue;
            defer sys.close(child);
            walkInventory(arena, child, relative, found);
            continue;
        }
        if (kind != fsx.DT.REG and kind != fsx.DT.LNK) continue;
        if (kind == fsx.DT.LNK) {
            const file = fsx.openRegular(dir, entry.name, true) catch continue;
            sys.close(file.fd);
        }
        const base = entry.name;
        if (base.len == 0 or base[0] == '.' or std.mem.eql(u8, base, "default") or std.mem.eql(u8, base, "modulerc")) continue;
        var name = relative;
        if (std.mem.endsWith(u8, base, ".lua")) {
            name = name[0 .. name.len - 4];
        }
        if (text.moduleName(name)) found.append(arena, arena.dupe(u8, name) catch oom()) catch oom();
    }
}

fn inventoryModules(arena: Allocator, root: []const u8) []const []const u8 {
    const dir = fsx.openDir(fsx.cwd, root, true) catch return &.{};
    defer sys.close(dir);
    var found: std.ArrayList([]const u8) = .empty;
    walkInventory(arena, dir, "", &found);
    std.mem.sort([]const u8, found.items, {}, struct {
        fn less(_: void, a: []const u8, b: []const u8) bool {
            return std.mem.order(u8, a, b) == .lt;
        }
    }.less);
    return found.items;
}

/// `modules/all` of the user and project trees EESSI-extend installs into,
/// for `target`: `PREFIX/versions/EESSI_VERSION/software/OS/TARGET/modules/all`
/// for `EESSI_USER_INSTALL` and `EESSI_PROJECT_INSTALL`, and for `HOME/eessi`,
/// EESSI-extend's default user prefix, when `EESSI_USER_INSTALL` is not in the
/// environment. Unsafe values are skipped.
fn extendModuleRoots(arena: Allocator, env: envmap.Map, os_type: []const u8, target: []const u8) []const []const u8 {
    const eessi_version = env.get("EESSI_VERSION") orelse "";
    if (!text.safeRelSubdir(eessi_version)) return &.{};
    var bases: std.ArrayList([]const u8) = .empty;
    for ([_][]const u8{ "EESSI_USER_INSTALL", "EESSI_PROJECT_INSTALL" }) |name| {
        const value = env.get(name) orelse "";
        if (value.len > 0 and text.safeAbsolute(value)) bases.append(arena, value) catch oom();
    }
    const home = env.get("HOME") orelse "";
    if (env.get("EESSI_USER_INSTALL") == null and home.len > 0 and text.safeAbsolute(home))
        bases.append(arena, std.fs.path.join(arena, &.{ home, "eessi" }) catch oom()) catch oom();
    var roots: std.ArrayList([]const u8) = .empty;
    outer: for (bases.items) |base| {
        const root = std.fs.path.join(arena, &.{ base, "versions", eessi_version, "software", os_type, target, "modules", "all" }) catch oom();
        for (roots.items) |seen| if (std.mem.eql(u8, seen, root)) continue :outer;
        roots.append(arena, root) catch oom();
    }
    return roots.items;
}

fn writeSubmitFile(path: []const u8, data: []const u8) bool {
    const fd = sys.openat(fsx.cwd, path, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .NOFOLLOW = true }, 0o600) catch return false;
    defer sys.close(fd);
    sys.writeAll(fd, data) catch return false;
    return true;
}

fn checkSubmitSemantic(code: u8) []const u8 {
    if (code == 0) return "ok";
    if (code == 1) return "missing";
    return "invalid";
}

fn optSelector(s: []const u8) ?[]const u8 {
    return if (s.len > 0) s else null;
}

/// One ASCII JSON line for ``--check-submit --json``, schema_version 1.
fn renderCheckSubmitJson(
    arena: Allocator,
    code: u8,
    partition: []const u8,
    constraint: []const u8,
    target: ?[]const u8,
    loaded: ?usize,
    missing: []const []const u8,
    message: ?[]const u8,
) Allocator.Error![]u8 {
    var missing_list: std.ArrayList(Py) = .empty;
    for (missing) |name| try missing_list.append(arena, Py.s(name));
    const loaded_value: Py = if (loaded) |n| .{ .int = @intCast(n) } else .null;
    var top: std.ArrayList(Py.Entry) = .empty;
    try top.append(arena, .{ .key = "constraint", .value = Py.opt(optSelector(constraint)) });
    try top.append(arena, .{ .key = "exit", .value = .{ .int = code } });
    try top.append(arena, .{ .key = "exit_semantic", .value = Py.s(checkSubmitSemantic(code)) });
    try top.append(arena, .{ .key = "kind", .value = Py.s("check-submit") });
    try top.append(arena, .{ .key = "loaded", .value = loaded_value });
    try top.append(arena, .{ .key = "message", .value = Py.opt(message) });
    try top.append(arena, .{ .key = "missing", .value = .{ .list = missing_list.items } });
    try top.append(arena, .{ .key = "partition", .value = Py.opt(optSelector(partition)) });
    try top.append(arena, .{ .key = "schema_version", .value = .{ .int = jsonout.schema_version } });
    try top.append(arena, .{ .key = "target", .value = Py.opt(target) });
    const pretty = try json.dumps(arena, .{ .dict = top.items }, 0, true);
    const compact = try compactJsonLine(arena, pretty);
    const line = try pythonJsonCommas(arena, compact);
    return try std.fmt.allocPrint(arena, "{s}\n", .{line});
}

fn listed(missing: []const []const u8, name: []const u8) bool {
    for (missing) |item| if (std.mem.eql(u8, item, name)) return true;
    return false;
}

fn finishCheckSubmit(
    arena: Allocator,
    args: Args,
    partition: []const u8,
    constraint: []const u8,
    code: u8,
    target: ?[]const u8,
    loaded: ?usize,
    missing: []const []const u8,
    message: ?[]const u8,
    stderr_lines: []const []const u8,
) u8 {
    for (stderr_lines) |line| writeMessage(arena, 2, line);
    if (args.json) {
        const body = renderCheckSubmitJson(arena, code, partition, constraint, target, loaded, missing, message) catch oom();
        cli.put(1, body);
    } else if (code == 0) {
        if (message) |msg| cli.put(1, arena.print("eessi-arch-guard: check-submit: {s}\n", .{msg}) catch "");
    }
    return code;
}

fn checkSubmit(arena: Allocator, args: Args, env: envmap.Map, settings: ?Settings) u8 {
    const partition = args.partition orelse "";
    const constraint = args.constraint orelse "";
    var why: []const u8 = "";
    const target = submitTarget(arena, settings, partition, constraint, &why) orelse {
        const line = arena.print("eessi-arch-guard: check-submit: {s}", .{why}) catch why;
        return finishCheckSubmit(arena, args, partition, constraint, 2, null, null, &.{}, why, &.{line});
    };
    const loaded = loadedSubmitModules(arena, env, &why) orelse {
        const line = arena.print("eessi-arch-guard: check-submit: {s}", .{why}) catch why;
        return finishCheckSubmit(arena, args, partition, constraint, 2, null, null, &.{}, why, &.{line});
    };
    const prefix = env.get("EESSI_PREFIX") orelse "";
    const os_type = env.get("EESSI_OS_TYPE") orelse "";
    if (!text.safeAbsolute(prefix) or !text.safeRelSubdir(os_type)) {
        const reason = "EESSI_PREFIX or EESSI_OS_TYPE is missing or unsafe";
        const line = "eessi-arch-guard: check-submit: " ++ reason;
        return finishCheckSubmit(arena, args, partition, constraint, 2, null, @intCast(loaded.len), &.{}, reason, &.{line});
    }
    const configured = if (settings) |s| s.get("eb_stack") orelse "" else "";
    const selected = args.eb_stack orelse (if (configured.len > 0) configured else "eb-stack");
    const eb_stack = findExecutable(arena, env, selected) orelse {
        const shown = text.oneLine(arena, selected, 200) catch selected;
        const reason = arena.print("eb-stack is not executable: {s}", .{shown}) catch shown;
        const line = arena.print("eessi-arch-guard: check-submit: {s}", .{reason}) catch reason;
        return finishCheckSubmit(arena, args, partition, constraint, 2, null, @intCast(loaded.len), &.{}, reason, &.{line});
    };
    const module_root = std.fs.path.join(arena, &.{ prefix, "software", os_type, target, "modules", "all" }) catch oom();
    var all_found: std.ArrayList([]const u8) = .empty;
    all_found.appendSlice(arena, inventoryModules(arena, module_root)) catch oom();
    for (extendModuleRoots(arena, env, os_type, target)) |root| {
        for (inventoryModules(arena, root)) |name| {
            const seen = for (all_found.items) |have| {
                if (std.mem.eql(u8, have, name)) break true;
            } else false;
            if (!seen) all_found.append(arena, name) catch oom();
        }
    }
    std.mem.sort([]const u8, all_found.items, {}, struct {
        fn less(_: void, a: []const u8, b: []const u8) bool {
            return std.mem.order(u8, a, b) == .lt;
        }
    }.less);
    const installed = all_found.items;
    const work = submitWorkDir(arena) orelse {
        const reason = "could not create temporary input directory";
        const line = "eessi-arch-guard: check-submit: " ++ reason;
        return finishCheckSubmit(arena, args, partition, constraint, 2, target, @intCast(loaded.len), &.{}, reason, &.{line});
    };
    defer fsx.deleteTree(arena, fsx.cwd, work) catch {};
    const easystack = std.fs.path.join(arena, &.{ work, "loaded.yml" }) catch oom();
    const installed_path = std.fs.path.join(arena, &.{ work, "installed.txt" }) catch oom();
    const unsupported = std.fs.path.join(arena, &.{ work, "unsupported.toml" }) catch oom();
    var plan: std.ArrayList(u8) = .empty;
    plan.appendSlice(arena, "easyconfigs:\n") catch oom();
    for (loaded) |module| plan.print(arena, "  - {s}\n", .{module.file}) catch oom();
    var inventory: std.ArrayList(u8) = .empty;
    for (installed) |name| inventory.print(arena, "{s}\n", .{plannerModule(arena, name)}) catch oom();
    if (!writeSubmitFile(easystack, plan.items) or !writeSubmitFile(installed_path, inventory.items) or !writeSubmitFile(unsupported, "")) {
        const reason = "could not write temporary planner inputs";
        const line = "eessi-arch-guard: check-submit: " ++ reason;
        return finishCheckSubmit(arena, args, partition, constraint, 2, target, @intCast(loaded.len), &.{}, reason, &.{line});
    }
    const child_env = envmap.toEnvp(arena, env) catch {
        const reason = "could not prepare eb-stack environment";
        const line = "eessi-arch-guard: check-submit: " ++ reason;
        return finishCheckSubmit(arena, args, partition, constraint, 2, target, @intCast(loaded.len), &.{}, reason, &.{line});
    };
    const child_args = [_][]const u8{ eb_stack, "stack", "arch", "--arch", target, "--easystack", easystack, "--installed", installed_path, "--unsupported", unsupported };
    const result = proc.run(arena, eb_stack, &child_args, child_env, .{
        .stdout_limit = 8 << 20,
        .stderr_limit = 8 << 20,
        .timeout_ms = null,
        .new_session = false,
    }) catch {
        const reason = "could not run eb-stack";
        const line = "eessi-arch-guard: check-submit: " ++ reason;
        return finishCheckSubmit(arena, args, partition, constraint, 2, target, @intCast(loaded.len), &.{}, reason, &.{line});
    };
    const code: u8 = switch (result.term) {
        .exited => |value| value,
        .signaled => 2,
    };
    if (code == 0) {
        const summary = arena.print("{d} loaded module(s) available for {s}", .{ loaded.len, target }) catch "";
        return finishCheckSubmit(arena, args, partition, constraint, 0, target, @intCast(loaded.len), &.{}, summary, &.{});
    }
    var missing: std.ArrayList([]const u8) = .empty;
    var lines_out: std.ArrayList([]const u8) = .empty;
    var stdout_lines = std.mem.splitScalar(u8, result.stdout, '\n');
    while (stdout_lines.next()) |line| {
        const marker = " is flagged on ";
        const cut = std.mem.indexOf(u8, line, marker) orelse continue;
        const file = line[0..cut];
        for (loaded) |module| {
            if (!std.mem.eql(u8, file, module.file)) continue;
            if (listed(missing.items, module.name)) continue;
            missing.append(arena, module.name) catch oom();
            lines_out.append(arena, arena.print("eessi-arch-guard: check-submit: loaded module absent from {s}: {s}", .{ target, module.name }) catch module.name) catch oom();
        }
    }
    if (code == 1 and missing.items.len > 0) {
        return finishCheckSubmit(arena, args, partition, constraint, 1, target, @intCast(loaded.len), missing.items, null, lines_out.items);
    }
    const raw = if (result.stderr.len > 0) result.stderr else if (result.stdout.len > 0) result.stdout else "eb-stack returned no detail";
    const shown = text.oneLine(arena, raw, 300) catch raw;
    const reason = arena.print("eb-stack failed: {s}", .{shown}) catch shown;
    const line = arena.print("eessi-arch-guard: check-submit: {s}", .{reason}) catch reason;
    return finishCheckSubmit(arena, args, partition, constraint, 2, target, @intCast(loaded.len), &.{}, reason, &.{line});
}

fn realMain(arena: Allocator, argv0: []const u8, argv: []const []const u8, envp: proc.Envp) u8 {
    const args = parseArgs(argv);
    const env = envmap.fromEnvp(arena, envp) catch emergencyExit("MemoryError", "out of memory");
    var ctx: guard.Ctx = .{
        .arena = arena,
        .env = env,
        .strict = emergency_strict,
        .self_exe = selfExe(arena),
    };
    if (args.restore) return guard.restoreMain(&ctx);
    if (args.help) {
        cli.put(1, help_text);
        return 0;
    }
    if (args.version) {
        cli.put(1, version_text);
        return 0;
    }

    var chain_out: []const u8 = "";
    const cfg = siteConfig(arena, invokedPath(arena, argv0));
    const config_status: []const u8 = if (cfg.settings != null)
        "loaded"
    else if (cfg.path != null or cfg.warning != null)
        "ignored"
    else
        "none";
    var notes: std.ArrayList([]const u8) = .empty;
    if (cfg.warning) |w| notes.append(arena, w) catch oom();
    if (args.check_submit) {
        for (notes.items) |n| writeMessage(arena, 2, n);
        return checkSubmit(arena, args, env, cfg.settings);
    }
    var chain: ?[]const u8 = null;
    var chain_path: ?[]const u8 = null;
    var chain_source: []const u8 = "none";
    var chain_status: []const u8 = "none";
    if (args.shell) {
        chain_status = "skipped";
    } else {
        chain = args.chain;
        if (chain == null) {
            chain = if (cfg.settings) |s| s.get("chain") else null;
            if (chain) |ch| {
                if (ch.len == 0) {
                    chain = null;
                } else {
                    chain_source = "config";
                    chain_path = ch;
                    if (chainPathOk(ch)) {
                        chain_status = "valid";
                    } else {
                        chain_status = "invalid";
                        notes.append(arena, arena.print("eessi-arch-guard: config chain is not an absolute executable: {s}", .{text.oneLine(arena, ch, 200) catch ch}) catch "") catch oom();
                        chain = null;
                    }
                }
            }
        } else {
            chain_source = "cli";
            chain_path = chain.?;
            if (chainPathOk(chain.?)) {
                chain_status = "valid";
            } else {
                chain_status = "invalid";
                notes.append(arena, "eessi-arch-guard: --chain is not an absolute executable") catch oom();
                chain = null;
            }
        }
    }
    for (notes.items) |n| writeMessage(arena, 2, n);
    if (chain) |path| {
        if (!args.dry_run) {
            const envp_c = envmap.toEnvp(arena, env) catch envp;
            const r = proc.run(arena, path, &.{path}, envp_c, .{
                .stdout_limit = 64 << 20,
                .stderr_limit = 64 << 20,
                .timeout_ms = null,
                .new_session = false,
            }) catch |err| emergencyExit("OSError", @errorName(err));
            if (r.stderr.len > 0) cli.put(2, r.stderr);
            // A chain killed by a signal is 1, as a negative returncode is.
            const code: u8 = switch (r.term) {
                .exited => |x| x,
                .signaled => 1,
            };
            chain_out = r.stdout;
            emergency_chain = chain_out;
            if (code != 0) {
                writeStdout(chain_out, "");
                return code;
            }
        }
    }

    const t = resolveTimeout(arena, args.timeout, cfg.settings);
    if (t.warning) |w| writeMessage(arena, 2, w);
    const sl = syslog.resolve(arena, args.syslog, env, cfg.settings);
    if (sl.warning) |w| writeMessage(arena, 2, w);

    if (args.dry_run) {
        const mode: []const u8 = if (args.shell) "shell" else "prolog";
        const announce = !args.shell and std.mem.eql(u8, env.get("SLURM_LOCALID") orelse "0", "0");
        const job_id = env.get("SLURM_JOB_ID") orelse empty;
        const step_id = env.get("SLURM_STEP_ID") orelse "batch";
        const local_id = env.get("SLURM_LOCALID");
        const cache_probe = guard.probeCacheReadonly(&ctx, env, args.shell);
        const decision = guard.limitedDryRun(&ctx, env, t.value) catch {
            emergencyWrite(guard.os_failure.class, guard.os_failure.text);
            return if (args.strict) strict_failure_exit else 0;
        };
        const exit_code: u8 = if (args.strict and decision.kind == .failed) strict_failure_exit else 0;
        const report: DryRunReport = .{
            .mode = mode,
            .strict = args.strict,
            .announce = announce,
            .config_path = cfg.path,
            .config_status = config_status,
            .config_warning = cfg.warning,
            .chain_path = chain_path,
            .chain_source = chain_source,
            .chain_status = chain_status,
            .timeout = t.value,
            .syslog_text = syslog.formatTarget(arena, sl.target),
            .syslog_all = sl.log_all,
            .syslog_pen = sl.pen,
            .job_id = job_id,
            .step_id = step_id,
            .local_id = local_id,
            .cache_path = cache_probe.path,
            .cache_hit = cache_probe.hit,
            .cached_kind = cache_probe.cached_kind,
            .decision = decision,
            .exit_code = exit_code,
        };
        const body = (if (args.json) renderDryRunJson(arena, report) else renderDryRunText(arena, report)) catch oom();
        cli.put(1, body);
        return exit_code;
    }

    // An OSError the decision does not catch: Python's main reports it as a
    // skip, after the chained output, and leaves the environment.
    const outcome = guard.limitedDecide(&ctx, env, args.shell, t.value) catch {
        const was = env.get("EESSI_SOFTWARE_SUBDIR") orelse "";
        const failed = guard.failureOutcome(
            arena,
            was,
            guard.os_failure.class,
            guard.os_failure.text,
            guard.os_failure.text,
        );
        syslog.emitEmergency(arena, sl, env, guard.os_failure.class, guard.os_failure.text, args.strict);
        const code: u8 = if (args.strict) strict_failure_exit else 0;
        if (args.json) {
            jsonout.emit(arena, failed, env, false, args.strict, args.shell, code);
        } else {
            writeStdout(chain_out, "");
            emergencyWrite(guard.os_failure.class, guard.os_failure.text);
        }
        return code;
    };
    const announce = !args.shell and std.mem.eql(u8, env.get("SLURM_LOCALID") orelse "0", "0");
    const body = envmap.render(arena, outcome.changes, outcome.message, args.shell, announce) catch |err| {
        const cls = if (err == error.OutOfMemory) "MemoryError" else "ValueError";
        const detail = envmap.renderErrorText(err);
        const was = env.get("EESSI_SOFTWARE_SUBDIR") orelse "";
        const failed = guard.failureOutcome(arena, was, cls, detail, detail);
        syslog.emitEmergency(arena, sl, env, cls, detail, args.strict);
        const code: u8 = if (args.strict) strict_failure_exit else 0;
        if (args.json) {
            jsonout.emit(arena, failed, env, false, args.strict, args.shell, code);
        } else {
            writeStdout(chain_out, "");
            emergencyWrite(cls, detail);
        }
        return code;
    };
    syslog.emitDecision(arena, sl, outcome, env, guard.last_cached, args.strict);
    if (args.shell) if (outcome.message) |m| if (m.len > 0) writeMessage(arena, 2, m);
    if (std.mem.eql(u8, env.get("EESSI_ARCH_GUARD_VERBOSE") orelse empty, "1")) if (outcome.detail) |d| if (d.len > 0) writeMessage(arena, 2, d);
    const code: u8 = if (args.strict and outcome.kind == .failed) strict_failure_exit else 0;
    if (args.json) {
        jsonout.emit(arena, outcome, env, guard.last_cached, args.strict, args.shell, code);
    } else {
        writeStdout(chain_out, body);
    }
    return code;
}

/// Run the command line and return the exit status. Before anything can
/// fail it notes whether this is local task 0 and whether --shell and
/// --strict were given, and installs the crash handlers, so that a crash
/// prints the skip line where that run would print it.
pub fn main(init: std.process.Init.Minimal) u8 {
    emergency_announce = localTaskZero(init.environ.block.slice.ptr);
    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    const arena = arena_state.allocator();
    const all = init.args.toSlice(arena) catch emergencyExit("MemoryError", "out of memory");
    const argv0: []const u8 = if (all.len > 0) all[0] else "";
    const argv = arena.alloc([]const u8, if (all.len > 0) all.len - 1 else 0) catch emergencyExit("MemoryError", "out of memory");
    for (argv, 0..) |*a, i| a.* = all[i + 1];
    emergency_shell = contains(argv, "--shell") or contains(argv, "--dry-run");
    emergency_strict = contains(argv, "--strict");
    installCrashHandlers();
    return realMain(arena, argv0, argv, init.environ.block.slice.ptr);
}

test {
    _ = text;
    _ = envmap;
    _ = guard;
    _ = syslog;
}

test "parseArgs" {
    const a = parseArgs(&.{ "--shell", "--chain", "--strict", "--timeout=5", "x" });
    try std.testing.expect(a.shell and a.strict);
    try std.testing.expectEqualStrings("", a.chain.?);
    try std.testing.expectEqualStrings("5", a.timeout.?);
    const b = parseArgs(&.{"--timeout"});
    try std.testing.expectEqualStrings("", b.timeout.?);
    const c = parseArgs(&.{ "--timeout", "-h", "--chain", "/bin/true", "--chain=-x" });
    try std.testing.expect(c.help);
    try std.testing.expectEqualStrings("", c.timeout.?);
    try std.testing.expectEqualStrings("-x", c.chain.?);
    const d = parseArgs(&.{ "--chain", "", "--timeout", "7" });
    try std.testing.expectEqualStrings("", d.chain.?);
    try std.testing.expectEqualStrings("7", d.timeout.?);
    const e = parseArgs(&.{ "--dry-run", "--json" });
    try std.testing.expect(e.dry_run and e.json);
    const f = parseArgs(&.{ "--check-submit", "--json", "--partition", "cpu", "--constraint=zen4", "--eb-stack", "/opt/bin/eb-stack" });
    try std.testing.expect(f.check_submit and f.json);
    try std.testing.expectEqualStrings("cpu", f.partition.?);
    try std.testing.expectEqualStrings("zen4", f.constraint.?);
    try std.testing.expectEqualStrings("/opt/bin/eb-stack", f.eb_stack.?);
}

test "submitTarget requires agreeing site mappings" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var settings: Settings = .empty;
    try settings.put(a, "submit_target.partition.cpu", "x86_64/amd/zen4");
    try settings.put(a, "submit_target.constraint.zen4", "x86_64/amd/zen4");
    try settings.put(a, "submit_target.constraint.zen2", "x86_64/amd/zen2");
    var why: []const u8 = "";
    try std.testing.expectEqualStrings("x86_64/amd/zen4", submitTarget(a, settings, "cpu", "zen4", &why).?);
    try std.testing.expect(submitTarget(a, settings, "cpu", "zen2", &why) == null);
    try std.testing.expect(std.mem.indexOf(u8, why, "different targets") != null);
}

test "renderCheckSubmitJson is schema_version 1" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const missing = [_][]const u8{"OnlyOther/1.0"};
    const absent = try renderCheckSubmitJson(a, 1, "cpu", "zen4", "x86_64/amd/zen4", 2, &missing, null);
    try std.testing.expectEqualStrings(
        "{\"constraint\": \"zen4\", \"exit\": 1, \"exit_semantic\": \"missing\", \"kind\": \"check-submit\", \"loaded\": 2, \"message\": null, \"missing\": [\"OnlyOther/1.0\"], \"partition\": \"cpu\", \"schema_version\": 1, \"target\": \"x86_64/amd/zen4\"}\n",
        absent,
    );
    const invalid = try renderCheckSubmitJson(a, 2, "", "", null, null, &.{}, "--partition needs one safe partition name");
    try std.testing.expectEqualStrings(
        "{\"constraint\": null, \"exit\": 2, \"exit_semantic\": \"invalid\", \"kind\": \"check-submit\", \"loaded\": null, \"message\": \"--partition needs one safe partition name\", \"missing\": [], \"partition\": null, \"schema_version\": 1, \"target\": null}\n",
        invalid,
    );
}

test "renderDryRunText and renderDryRunJson format a decision summary" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const mods = [_][]const u8{ "R/4.4.2", "GCC/13.2.0" };
    const report: DryRunReport = .{
        .mode = "prolog",
        .strict = false,
        .announce = true,
        .config_path = "/etc/eessi-arch-guard.conf",
        .config_status = "loaded",
        .config_warning = null,
        .chain_path = "/etc/slurm/task_prolog.site",
        .chain_source = "config",
        .chain_status = "valid",
        .timeout = 15.0,
        .syslog_text = null,
        .syslog_all = false,
        .syslog_pen = "32473",
        .job_id = "42",
        .step_id = "0",
        .local_id = "0",
        .cache_path = "/tmp/cache",
        .cache_hit = false,
        .cached_kind = null,
        .decision = .{
            .kind = .reloaded,
            .was_cpu = "x86_64/amd/zen4",
            .now_cpu = "x86_64/amd/zen2",
            .eessi = "EESSI/2025.06",
            .modules = &mods,
            .marker = "x86_64/amd/zen4->x86_64/amd/zen2",
            .message = "eessi-arch-guard: would reload modules for x86_64/amd/zen2 (loaded for x86_64/amd/zen4)",
        },
        .exit_code = 0,
    };
    const txt = try renderDryRunText(a, report);
    try std.testing.expect(std.mem.find(u8, txt, "config: /etc/eessi-arch-guard.conf (loaded)\n") != null);
    try std.testing.expect(std.mem.find(u8, txt, "chain: /etc/slurm/task_prolog.site (config, valid)\n") != null);
    try std.testing.expect(std.mem.find(u8, txt, "timeout: 15\n") != null);
    try std.testing.expect(std.mem.find(u8, txt, "decision: reloaded\n") != null);
    try std.testing.expect(std.mem.find(u8, txt, "modules: R/4.4.2 GCC/13.2.0\n") != null);
    const js = try renderDryRunJson(a, report);
    try std.testing.expect(std.mem.find(u8, js, "\"schema_version\"") != null);
    try std.testing.expect(std.mem.find(u8, js, "\"kind\"") != null);
    try std.testing.expect(std.mem.find(u8, js, "decision") != null);
    try std.testing.expect(std.mem.find(u8, js, "R/4.4.2") != null);
    try std.testing.expect(std.mem.find(u8, js, "GCC/13.2.0") != null);
    try std.testing.expect(std.mem.find(u8, js, "\"exit\": 0") != null);
    try std.testing.expect(std.mem.endsWith(u8, js, "\n"));
}

test "localTaskZero reads the first SLURM_LOCALID" {
    const first = [_:null]?[*:0]const u8{ "PATH=/bin", "SLURM_LOCALID=0", "SLURM_LOCALID=3" };
    const other = [_:null]?[*:0]const u8{"SLURM_LOCALID=3"};
    const blank = [_:null]?[*:0]const u8{"SLURM_LOCALID="};
    const unset = [_:null]?[*:0]const u8{"PATH=/bin"};
    try std.testing.expect(localTaskZero(&first));
    try std.testing.expect(!localTaskZero(&other));
    try std.testing.expect(!localTaskZero(&blank));
    try std.testing.expect(localTaskZero(&unset));
}

test "pyFloat follows Python's float()" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    try std.testing.expectEqual(@as(?f64, 1000.0), pyFloat(a, " 1_000 "));
    try std.testing.expectEqual(@as(?f64, 0.5), pyFloat(a, ".5"));
    try std.testing.expectEqual(@as(?f64, 5.0), pyFloat(a, "5."));
    try std.testing.expectEqual(@as(?f64, 2e3), pyFloat(a, "+2E3"));
    try std.testing.expect(std.math.isInf(pyFloat(a, "-Infinity").?));
    try std.testing.expect(pyFloat(a, "1__0") == null);
    try std.testing.expect(pyFloat(a, "_1") == null);
    try std.testing.expect(pyFloat(a, "1e") == null);
    try std.testing.expect(pyFloat(a, ".") == null);
    try std.testing.expect(pyFloat(a, "0x10") == null);
    try std.testing.expect(parseTimeout(a, "inf") == null);
    try std.testing.expect(parseTimeout(a, "0") == null);
    try std.testing.expect(parseTimeout(a, "2147483.648") == null);
    try std.testing.expect(parseTimeout(a, "9223372036") == null);
    try std.testing.expectEqual(@as(?f64, 2147483.647), parseTimeout(a, "2147483.647"));
}

test "parseConfigBytes keeps other keys after a bad value" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var bad: std.ArrayList([]const u8) = .empty;
    const s = try parseConfigBytes(a, "# c\r\nchain = /bin/true\ntimeout=\xff\n \nnoeq\n=x\ntimeout = 30\r", &bad);
    try std.testing.expectEqualStrings("/bin/true", s.get("chain").?);
    try std.testing.expectEqualStrings("30", s.get("timeout").?);
    try std.testing.expectEqual(@as(usize, 1), bad.items.len);
}

fn expectNormPath(gpa: Allocator, path: []const u8, want: []const u8) !void {
    const got = try normPath(gpa, path);
    defer gpa.free(got);
    try std.testing.expectEqualStrings(want, got);
}

test "normPath follows posixpath.normpath" {
    const cases = [_][2][]const u8{
        .{ "", "." },               .{ "/", "/" },             .{ "//", "//" },
        .{ "///", "/" },            .{ "//a/b", "//a/b" },     .{ "///a//b/", "/a/b" },
        .{ "/a/./b/../c", "/a/c" }, .{ "/..", "/" },           .{ "/../a", "/a" },
        .{ "a/../..", ".." },       .{ "../a/..", ".." },      .{ "./", "." },
        .{ "a/b/../../..", ".." },  .{ "/a/b/../../..", "/" }, .{ "//./x/../y", "//y" },
        .{ "a//b/./c/", "a/b/c" },
    };
    for (cases) |c| try expectNormPath(std.testing.allocator, c[0], c[1]);
    try std.testing.checkAllAllocationFailures(std.testing.allocator, expectNormPath, .{ "/a/./b/../c", "/a/c" });
}

fn expectConfigBeside(gpa: Allocator, invoked: []const u8, want: []const u8) !void {
    const got = try configBeside(gpa, invoked);
    defer gpa.free(got);
    try std.testing.expectEqualStrings(want, got);
}

test "configBeside is the config in the directory of the normalized path" {
    const t = std.testing.allocator;
    try expectConfigBeside(t, "/usr/local/bin/eessi-arch-guard", "/usr/local/bin/eessi-arch-guard.conf");
    try expectConfigBeside(t, "/eessi-arch-guard", "/eessi-arch-guard.conf");
    try expectConfigBeside(t, "//eessi-arch-guard", "//eessi-arch-guard.conf");
    try expectConfigBeside(t, "/opt/./x/../bin//eessi-arch-guard", "/opt/bin/eessi-arch-guard.conf");
    var buf: [sys.path_max]u8 = undefined;
    const cwd = try fsx.realpath(fsx.cwd, ".", &buf);
    const want = try std.mem.concat(t, u8, &.{ cwd, "/link/eessi-arch-guard.conf" });
    defer t.free(want);
    try expectConfigBeside(t, "./link/eessi-arch-guard", want);
    try std.testing.checkAllAllocationFailures(t, expectConfigBeside, .{ "link/../link/eessi-arch-guard", want });
}

test "extendModuleRoots follows EESSI-extend's user and project prefixes" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    const tail = "/versions/2026.06/software/linux/x86_64/amd/zen4/modules/all";

    var env: envmap.Map = .empty;
    try env.put(arena, "EESSI_VERSION", "2026.06");
    try env.put(arena, "HOME", "/home/alice");
    var roots = extendModuleRoots(arena, env, "linux", "x86_64/amd/zen4");
    try std.testing.expectEqual(@as(usize, 1), roots.len);
    try std.testing.expectEqualStrings("/home/alice/eessi" ++ tail, roots[0]);

    try env.put(arena, "EESSI_USER_INSTALL", "/u");
    try env.put(arena, "EESSI_PROJECT_INSTALL", "/p");
    roots = extendModuleRoots(arena, env, "linux", "x86_64/amd/zen4");
    try std.testing.expectEqual(@as(usize, 2), roots.len);
    try std.testing.expectEqualStrings("/u" ++ tail, roots[0]);
    try std.testing.expectEqualStrings("/p" ++ tail, roots[1]);

    // Set but empty is a user prefix to EESSI-extend: no default, nothing read for it.
    try env.put(arena, "EESSI_USER_INSTALL", "");
    roots = extendModuleRoots(arena, env, "linux", "x86_64/amd/zen4");
    try std.testing.expectEqual(@as(usize, 1), roots.len);
    try std.testing.expectEqualStrings("/p" ++ tail, roots[0]);

    try env.put(arena, "EESSI_VERSION", "../x");
    try std.testing.expectEqual(@as(usize, 0), extendModuleRoots(arena, env, "linux", "x86_64/amd/zen4").len);
}
