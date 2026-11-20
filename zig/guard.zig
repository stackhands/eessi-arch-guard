//! The decision: compare the loaded EESSI target with this node, and when
//! they differ reload the module environment through the restore helper
//! (this program run again with --restore) and return the delta. One
//! deadline covers the cache lock, both archdetect runs and the helper; a
//! SIGTERM during the decision kills the running child's process group.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const text = @import("text.zig");
const envmap = @import("envmap.zig");
const json = common.json;

const sys = common.sys;
const fsx = common.fsx;
const proc = common.proc;
const state = common.state;
const Allocator = std.mem.Allocator;
const empty: []const u8 = "";
const Map = envmap.Map;
const Changes = envmap.Changes;

/// DIAGNOSE_LIMIT: the most failed modules the one-call-each load collects
/// before it stops; the message then says "(stopped after 32)".
pub const diagnose_limit = 32;
const max_arch_stdout = 65536;
const cpuinfo_read_max = 16 * 1024 * 1024;
const cache_read_max = 8 * 1024 * 1024;
const lock_poll_ms = 50;

/// The variables whose values, with the task's processors and visible GPUs,
/// make up the digest in a cache file's name.
pub const key_fields = [_][]const u8{
    "EESSI_SOFTWARE_SUBDIR", "EESSI_ACCEL_SUBDIR", "LOADEDMODULES", "MODULEPATH",
    "EESSI_INIT_PREFIX",     "EESSI_PROC_CPUINFO", "PATH",
};

const waitpkg_marches = [_][]const u8{
    "alderlake",      "arrowlake",     "arrowlake_s",     "clearwaterforest", "diamondrapids",
    "grandridge",     "graniterapids", "graniterapids_d", "novalake",         "pantherlake",
    "sapphirerapids", "sierraforest",  "tremont",
};

/// What a decision came to.
pub const Kind = enum {
    /// Nothing was compared: EESSI is not loaded, the guard is turned off,
    /// the detection is pinned, or EESSI_ARCH_GUARD_MISSING=ignore covers a
    /// node with no software directory.
    skip,
    /// The node's targets are the loaded ones.
    noop,
    /// The modules were loaded again for this node.
    reloaded,
    /// A reload was needed and could not be made; only
    /// EESSI_ARCH_GUARD_FAILED changes.
    failed,
};

/// A decision's result, as the cache stores it.
pub const Outcome = struct {
    /// The variables to set, or to unset where the value is null.
    changes: Changes = .empty,
    /// The message: a print line in the prolog output, a line on stderr with
    /// --shell.
    message: ?[]const u8 = null,
    /// What the decision came to.
    kind: Kind,
    /// The reason EESSI_ARCH_GUARD_VERBOSE=1 writes to stderr.
    detail: ?[]const u8 = null,
};

/// The decision ran out of time, or SIGTERM arrived; neither is cached.
/// Deadline and Terminated stop a decision; Os is an OSError the Python
/// guard does not catch on the way up, which main reports as a skip, with
/// its text in `os_failure`.
pub const Stop = error{ Deadline, Terminated, Os };

/// The OSError behind error.Os, as Python's main names it on the skip line:
/// the class Python raises for its errno, and its str(), "[Errno N] text",
/// with ": 'PATH'" after it for an error on a path.
pub var os_failure: OsFailure = .{};
pub var last_cached: bool = false;

pub const OsFailure = struct { class: []const u8 = "OSError", text: []const u8 = "" };

/// The OSError subclass Python raises for errno `e` (PEP 3151).
fn osErrorClass(e: linux.E) []const u8 {
    return switch (e) {
        .PERM, .ACCES => "PermissionError",
        .NOENT => "FileNotFoundError",
        .NOTDIR => "NotADirectoryError",
        .ISDIR => "IsADirectoryError",
        .EXIST => "FileExistsError",
        .INTR => "InterruptedError",
        .SRCH => "ProcessLookupError",
        .CHILD => "ChildProcessError",
        .AGAIN, .ALREADY, .INPROGRESS => "BlockingIOError",
        .PIPE, .SHUTDOWN => "BrokenPipeError",
        .CONNABORTED => "ConnectionAbortedError",
        .CONNREFUSED => "ConnectionRefusedError",
        .CONNRESET => "ConnectionResetError",
        .TIMEDOUT => "TimeoutError",
        else => "OSError",
    };
}

// ---- the deadline and SIGTERM ------------------------------------------

/// DecisionLimit: the deadline of one decision and the SIGTERM it may get.
pub const Limit = struct {
    /// The `nowMs` reading at which the decision stops.
    deadline_ms: i64,
    /// Set by the SIGTERM handler.
    terminated: std.atomic.Value(bool) = .init(false),
    /// Set while a finished decision is written to the cache, which neither
    /// the deadline nor SIGTERM interrupts.
    committing: std.atomic.Value(bool) = .init(false),
    /// The process group of the command the decision is running, 0 for none.
    child_pgid: std.atomic.Value(i32) = .init(0),
    /// The SIGTERM action to put back when the decision ends.
    previous: linux.Sigaction = undefined,

    /// Milliseconds left before the deadline; zero or less once it passed.
    pub fn remainingMs(l: *const Limit) i64 {
        return l.deadline_ms - nowMs();
    }

    /// error.Terminated after a SIGTERM, error.Deadline once no time is
    /// left; neither while the result is being written to the cache.
    pub fn check(l: *const Limit) Stop!void {
        if (l.committing.load(.seq_cst)) return;
        if (l.terminated.load(.seq_cst)) return error.Terminated;
        if (l.remainingMs() <= 0) return error.Deadline;
    }
};

var active_limit: ?*Limit = null;

/// Stop the process group of a command the decision is running, if any,
/// before an emergency exit; system calls only, safe in a handler.
pub fn stopChild() void {
    const l = active_limit orelse return;
    common.proc.stopGroup(l.child_pgid.load(.seq_cst));
}

fn onTerm(_: linux.SIG) callconv(.c) void {
    const l = active_limit orelse return;
    l.terminated.store(true, .seq_cst);
    if (l.committing.load(.seq_cst)) return;
    common.proc.killGroup(l.child_pgid.load(.seq_cst), .KILL);
}

fn installLimit(l: *Limit) void {
    active_limit = l;
    var act: linux.Sigaction = .{ .handler = .{ .handler = onTerm }, .mask = linux.sigemptyset(), .flags = 0 };
    _ = linux.sigaction(.TERM, &act, &l.previous);
}

fn removeLimit(l: *Limit) void {
    _ = linux.sigaction(.TERM, &l.previous, null);
    active_limit = null;
}

/// The monotonic clock in milliseconds, as Python's time.monotonic() counts.
pub fn nowMs() i64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(.MONOTONIC, &ts);
    return @as(i64, ts.sec) * 1000 + @divTrunc(@as(i64, ts.nsec), 1_000_000);
}

// ---- context -----------------------------------------------------------

/// What every step of a decision needs.
pub const Ctx = struct {
    /// Holds everything the decision allocates; freed when the program ends.
    arena: Allocator,
    /// The task's environment.
    env: Map,
    /// --strict is on the command line: the restore and the cache use only a
    /// private workspace.
    strict: bool,
    /// Absolute path of this program, for the restore helper.
    self_exe: []const u8,
    /// The deadline while `limitedDecide` runs; null otherwise, as in the
    /// restore helper, which its parent's deadline covers.
    limit: ?*Limit = null,
};

// ---- running programs ----------------------------------------------------

/// Why a command gave no result: the decision stopped, the command could not
/// be started, or something no message was written for.
pub const RunError = Stop || error{ CannotRun, Unexpected };

/// A command that ran to the end.
pub const Ran = struct {
    /// The exit status, or 128 plus the signal that ended it.
    code: u32,
    /// What the command wrote to stdout; more than 64 MiB is an error.
    stdout: []const u8,
    /// What the command wrote to stderr; more than 1 MiB is an error.
    stderr: []const u8,
};

/// Default search path matching Python `os.defpath` (`Lib/posixpath.py`)
/// and `shutil.which` (`Lib/shutil.py`) when `PATH` is unset.
pub const default_path = "/bin:/usr/bin";

/// Look up `name` along `env`'s `PATH`, defaulting to `default_path` when
/// `PATH` is unset. Empty `:`-delimited entries search `.` in `proc.which`,
/// matching Python `shutil.which` (`Lib/shutil.py`: `path.split(os.pathsep)`
/// and `os.path.join("", cmd)`); a completely empty `PATH=""` also searches
/// `.` via `proc.which` here so `resolve` keeps POSIX `execvp` parity.
pub fn resolvePath(arena: Allocator, name: []const u8, env: Map) ?[]const u8 {
    const path = env.get("PATH") orelse default_path;
    return proc.which(arena, name, path) catch null;
}

/// The executable `name` would run as from an environment with `env`'s PATH
/// (os.defpath when it has none), as subprocess does; a name with '/' is a
/// path joined against cwd unchecked.
fn resolve(c: *Ctx, name: []const u8, env: Map) ?[]const u8 {
    if (std.mem.findScalar(u8, name, '/') != null) {
        if (name[0] == '/') return name;
        var rb: [sys.path_max]u8 = undefined;
        const cwd = fsx.realpath(fsx.cwd, ".", &rb) catch return null;
        return std.fs.path.join(c.arena, &.{ cwd, name }) catch null;
    }
    return resolvePath(c.arena, name, env);
}

/// The message subprocess raises for a program that cannot be found.
fn notFound(c: *Ctx, name: []const u8) []const u8 {
    return c.arena.print("[Errno 2] No such file or directory: '{s}'", .{name}) catch "could not start";
}

/// run_captured: under the decision limit with `new_session`, the child's
/// session is killed at the deadline or on SIGTERM.
fn runCaptured(c: *Ctx, argv: []const []const u8, env: Map, input: ?[]const u8, new_session: bool, why: *[]const u8) RunError!Ran {
    const exe = resolve(c, argv[0], env) orelse {
        why.* = notFound(c, argv[0]);
        return error.CannotRun;
    };
    const envp = envmap.toEnvp(c.arena, env) catch {
        why.* = "embedded null byte";
        return error.CannotRun;
    };
    const limited = c.limit != null and new_session;
    if (limited) try c.limit.?.check();
    var opts: proc.Options = .{
        .stdin = input,
        .stdout_limit = 64 << 20,
        .stderr_limit = 1 << 20,
        .timeout_ms = null,
        .new_session = limited,
    };
    if (limited) {
        const l = c.limit.?;
        const left = l.remainingMs();
        if (left <= 0) return error.Deadline;
        opts.timeout_ms = @intCast(@min(left, std.math.maxInt(u32)));
        opts.grace_ms = 0;
        opts.cancel = &l.terminated;
        opts.pgid_out = &l.child_pgid;
    }
    const r = proc.run(c.arena, exe, argv, envp, opts) catch |err| switch (err) {
        error.TimedOut => return error.Deadline,
        error.Canceled => return error.Terminated,
        error.NotFound => {
            why.* = notFound(c, argv[0]);
            return error.CannotRun;
        },
        else => {
            why.* = @errorName(err);
            return error.CannotRun;
        },
    };
    if (limited) {
        const l = c.limit.?;
        if (l.terminated.load(.seq_cst) and !l.committing.load(.seq_cst)) return error.Terminated;
    }
    return .{ .code = r.term.code(), .stdout = r.stdout, .stderr = r.stderr };
}

fn runBash(c: *Ctx, args: []const []const u8, env: Map, why: *[]const u8) RunError!Ran {
    var argv: std.ArrayList([]const u8) = .empty;
    argv.appendSlice(c.arena, &.{ "bash", "--noprofile", "--norc", "--" }) catch return error.Unexpected;
    argv.appendSlice(c.arena, args) catch return error.Unexpected;
    const stripped = envmap.stripStartup(c.arena, env) catch return error.Unexpected;
    return runCaptured(c, argv.items, stripped, null, c.limit != null, why);
}

// ---- probes ------------------------------------------------------------

/// What `probeCpu` found.
pub const CpuProbe = struct {
    /// `ok`: a target was found; `none`: no candidate has a software
    /// directory; `error`: the detection could not be run or read.
    status: enum { ok, none, @"error" },
    /// This node's CPU target, set when the status is `ok`.
    target: []const u8 = "",
    /// The detection's candidates, best first, without those the waitpkg rule
    /// drops.
    candidates: []const []const u8 = &.{},
    /// Why the status is `error`.
    err: []const u8 = "",
    /// The target again when the waitpkg rule changed the choice, for the
    /// reload to pin with EESSI_SOFTWARE_SUBDIR_OVERRIDE; empty otherwise.
    override: []const u8 = "",
};

/// What `probeAccel` found.
pub const AccelProbe = struct {
    /// `ok`: an accelerator subdirectory; `none`: no accelerator; `error`:
    /// the detection could not be run or printed something else.
    status: enum { ok, none, @"error" },
    /// The accelerator subdirectory, such as accel/nvidia/cc90.
    subdir: []const u8 = "",
    /// Why the status is `error`.
    err: []const u8 = "",
};

fn isFile(path: []const u8) bool {
    const st = fsx.statAt(fsx.cwd, path, true) catch return false;
    return st.isReg();
}

fn isDir(path: []const u8) bool {
    const st = fsx.statAt(fsx.cwd, path, true) catch return false;
    return st.isDir();
}

/// The first line of text as str.splitlines() splits it.
fn firstLine(s: []const u8) ?[]const u8 {
    if (s.len == 0) return null;
    var i: usize = 0;
    while (i < s.len) {
        const ch = text.nextChar(s[i..]);
        if (ch.cp) |cp| switch (cp) {
            '\n', '\r', 0x0b, 0x0c, 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029 => return s[0..i],
            else => {},
        };
        i += ch.len;
    }
    return s;
}

/// Lines as str.splitlines() returns them.
fn splitLines(arena: Allocator, s: []const u8) Allocator.Error![]const []const u8 {
    var out: std.ArrayList([]const u8) = .empty;
    var start: usize = 0;
    var i: usize = 0;
    while (i < s.len) {
        const ch = text.nextChar(s[i..]);
        const brk = if (ch.cp) |cp| switch (cp) {
            '\n', '\r', 0x0b, 0x0c, 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029 => true,
            else => false,
        } else false;
        if (brk) {
            try out.append(arena, s[start..i]);
            i += ch.len;
            if (ch.cp.? == '\r' and i < s.len and s[i] == '\n') i += 1;
            start = i;
            continue;
        }
        i += ch.len;
    }
    if (start < s.len) try out.append(arena, s[start..]);
    return out.items;
}

fn charCount(s: []const u8) usize {
    var n: usize = 0;
    var i: usize = 0;
    while (i < s.len) : (n += 1) i += text.nextChar(s[i..]).len;
    return n;
}

/// parse_cpu_line: exactly one non-empty line without controls, split at ':'.
fn parseCpuLine(arena: Allocator, stdout: []const u8) Allocator.Error!?[]const []const u8 {
    if (std.mem.findScalar(u8, stdout, 0) != null or charCount(stdout) > max_arch_stdout) return null;
    var nonempty: std.ArrayList([]const u8) = .empty;
    for (try splitLines(arena, stdout)) |l| if (l.len > 0) try nonempty.append(arena, l);
    if (nonempty.items.len != 1) return null;
    const line = nonempty.items[0];
    for (line) |b| if (b < 32) return null;
    var parts: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, line, ':');
    while (it.next()) |p| try parts.append(arena, p);
    return parts.items;
}

/// read_cpu_flags: the flags every `flags:` line of a cpuinfo file shares.
fn readCpuFlags(arena: Allocator, path: []const u8) []const []const u8 {
    if (path.len == 0) return &.{};
    const fd = sys.openat(fsx.cwd, path, .{ .NONBLOCK = true }, 0) catch return &.{};
    defer sys.close(fd);
    var data: std.ArrayList(u8) = .empty;
    var buf: [64 * 1024]u8 = undefined;
    while (data.items.len < cpuinfo_read_max) {
        const n = sys.read(fd, &buf) catch return &.{};
        if (n == 0) break;
        data.appendSlice(arena, buf[0..@min(n, cpuinfo_read_max - data.items.len)]) catch return &.{};
    }
    var common_set: ?std.StringArrayHashMapUnmanaged(void) = null;
    var it = std.mem.splitScalar(u8, data.items, '\n');
    while (it.next()) |line| {
        if (line.len < 5 or !std.ascii.eqlIgnoreCase(line[0..5], "flags")) continue;
        var i: usize = 5;
        while (i < line.len and (line[i] == ' ' or line[i] == '\t')) i += 1;
        if (i >= line.len or line[i] != ':') continue;
        i += 1;
        var set: std.StringArrayHashMapUnmanaged(void) = .empty;
        // The file is decoded with 'replace' and split on Unicode whitespace.
        const rest = text.decodeReplace(arena, line[i..]) catch return &.{};
        for (text.pySplit(arena, rest) catch return &.{}) |flag| set.put(arena, flag, {}) catch return &.{};
        if (common_set) |*cs| {
            var keep: std.StringArrayHashMapUnmanaged(void) = .empty;
            for (cs.keys()) |k| if (set.contains(k)) keep.put(arena, k, {}) catch return &.{};
            cs.* = keep;
        } else common_set = set;
    }
    return if (common_set) |cs| cs.keys() else &.{};
}

fn candidateSupportsCpu(candidate: []const u8, flags: []const []const u8) bool {
    const leaf = candidate[if (std.mem.findScalarLast(u8, candidate, '/')) |i| i + 1 else 0..];
    var buf: [128]u8 = undefined;
    if (leaf.len > buf.len) return true;
    const march = buf[0..leaf.len];
    for (leaf, 0..) |ch, i| march[i] = if (ch == '-') '_' else ch;
    if (!text.inList(&waitpkg_marches, march)) return true;
    return text.inList(flags, "waitpkg");
}

fn decodeReplace(c: *Ctx, s: []const u8) []const u8 {
    return text.decodeReplace(c.arena, s) catch s;
}

fn line1(c: *Ctx, s: []const u8, limit: usize) []const u8 {
    return text.oneLine(c.arena, s, limit) catch s;
}

// sched_getaffinity and cpuset lists name processors in this range. A bind
// mask with a bit past it is not a CPU set: applying the low bits alone
// would hide a processor the mask named.
const cpu_id_max: usize = 4096;

const CpuSet = struct {
    words: [cpu_id_max / 64]u64 = std.mem.zeroes([cpu_id_max / 64]u64),

    fn add(s: *CpuSet, cpu: usize) void {
        if (cpu >= cpu_id_max) return;
        s.words[cpu / 64] |= @as(u64, 1) << @intCast(cpu % 64);
    }

    fn has(s: CpuSet, cpu: usize) bool {
        if (cpu >= cpu_id_max) return false;
        return s.words[cpu / 64] & (@as(u64, 1) << @intCast(cpu % 64)) != 0;
    }

    fn bitAnd(a: CpuSet, b: CpuSet) CpuSet {
        var out: CpuSet = .{};
        for (a.words, 0..) |word, i| out.words[i] = word & b.words[i];
        return out;
    }

    fn count(s: CpuSet) usize {
        var n: usize = 0;
        for (s.words) |w| n += @popCount(w);
        return n;
    }

    fn eql(a: CpuSet, b: CpuSet) bool {
        return std.mem.eql(u64, &a.words, &b.words);
    }

    fn sortedIds(s: CpuSet, arena: Allocator) Allocator.Error![]usize {
        var out: std.ArrayList(usize) = .empty;
        for (s.words, 0..) |word, wi| {
            var w = word;
            while (w != 0) : (w &= w - 1) try out.append(arena, wi * 64 + @ctz(w));
        }
        return out.toOwnedSlice(arena);
    }
};

fn parseUsize(s: []const u8) ?usize {
    if (s.len == 0) return null;
    var n: usize = 0;
    for (s) |ch| {
        if (ch < '0' or ch > '9') return null;
        const d: usize = ch - '0';
        if (n > (std.math.maxInt(usize) - d) / 10) return null;
        n = n * 10 + d;
    }
    return n;
}

/// Bits of one hex mask, without the `0x` prefix. False when `hex` is empty,
/// holds a non-hex digit, or names a bit at or past `cpu_id_max`.
fn addHexMask(set: *CpuSet, hex: []const u8) bool {
    if (hex.len == 0) return false;
    var bit: usize = 0;
    var i: usize = hex.len;
    while (i > 0) {
        i -= 1;
        const val: u8 = switch (hex[i]) {
            '0'...'9' => hex[i] - '0',
            'a'...'f' => hex[i] - 'a' + 10,
            'A'...'F' => hex[i] - 'A' + 10,
            else => return false,
        };
        var b: u3 = 0;
        while (true) : (b += 1) {
            if (val & (@as(u8, 1) << b) != 0) {
                if (bit >= cpu_id_max) return false;
                set.add(bit);
            }
            bit += 1;
            if (b == 3) break;
        }
    }
    return true;
}

/// parse_cpu_spec: a hex mask, a 0-3,8 list, or a cpu[0-3] hostlist.
/// A comma-separated list of hex masks is their union, which is the string
/// Slurm stores for --cpu-bind=mask_cpu.
fn parseCpuSpec(spec: []const u8) ?CpuSet {
    const raw = std.mem.trim(u8, spec, " \t\r\n");
    if (raw.len == 0) return null;
    if (std.mem.startsWith(u8, raw, "0x") or std.mem.startsWith(u8, raw, "0X")) {
        var set: CpuSet = .{};
        var i: usize = 0;
        var saw = false;
        var trailing_comma = false;
        while (i < raw.len) {
            if (raw[i] == ' ') {
                i += 1;
                continue;
            }
            trailing_comma = false;
            if (i + 2 > raw.len or raw[i] != '0' or (raw[i + 1] != 'x' and raw[i + 1] != 'X')) return null;
            i += 2;
            const start = i;
            while (i < raw.len and raw[i] != ',' and raw[i] != ' ') i += 1;
            if (!addHexMask(&set, raw[start..i])) return null;
            saw = true;
            if (i < raw.len and raw[i] == ' ') {
                while (i < raw.len and raw[i] == ' ') i += 1;
            }
            if (i < raw.len) {
                if (raw[i] != ',') return null;
                trailing_comma = true;
                i += 1;
            }
        }
        if (!saw or trailing_comma) return null;
        return set;
    }
    var body = raw;
    if (std.mem.endsWith(u8, raw, "]")) {
        const open = std.mem.indexOfScalar(u8, raw, '[') orelse return null;
        if (std.mem.indexOfScalar(u8, raw[open + 1 ..], '[') != null) return null;
        const name = raw[0..open];
        if (name.len != 0 and !std.ascii.eqlIgnoreCase(name, "cpu")) return null;
        body = raw[open + 1 .. raw.len - 1];
    } else if (std.mem.indexOfScalar(u8, raw, '[') != null) {
        return null;
    }
    if (body.len == 0) return null;
    var set: CpuSet = .{};
    var i: usize = 0;
    var saw = false;
    var trailing_comma = false;
    while (i < body.len) {
        if (body[i] == ' ') {
            i += 1;
            continue;
        }
        trailing_comma = false;
        const start_i = i;
        while (i < body.len and body[i] >= '0' and body[i] <= '9') i += 1;
        const start = parseUsize(body[start_i..i]) orelse return null;
        if (start >= cpu_id_max) return null;
        if (i < body.len and body[i] == '-') {
            i += 1;
            const end_i = i;
            while (i < body.len and body[i] >= '0' and body[i] <= '9') i += 1;
            const end = parseUsize(body[end_i..i]) orelse return null;
            if (end < start or end >= cpu_id_max) return null;
            var cpu = start;
            while (cpu <= end) : (cpu += 1) set.add(cpu);
        } else set.add(start);
        saw = true;
        if (i < body.len and body[i] == ' ') {
            while (i < body.len and body[i] == ' ') i += 1;
        }
        if (i < body.len) {
            if (body[i] != ',') return null;
            trailing_comma = true;
            i += 1;
        }
    }
    if (!saw or trailing_comma) return null;
    return set;
}

fn readFile(arena: Allocator, path: []const u8, max: usize) ?[]u8 {
    const fd = sys.openat(fsx.cwd, path, .{ .NONBLOCK = true }, 0) catch return null;
    defer sys.close(fd);
    return fsx.readAll(arena, fd, max) catch null;
}

fn hasCgroupController(controllers: []const u8, wanted: []const u8) bool {
    var it = std.mem.splitScalar(u8, controllers, ',');
    while (it.next()) |name| if (std.mem.eql(u8, name, wanted)) return true;
    return false;
}

fn cgroupDirectoriesFrom(arena: Allocator, cgroup_text: []const u8, cgroup_root: []const u8) []const []const u8 {
    const trimmed_root = std.mem.trimEnd(u8, cgroup_root, "/");
    const root = if (trimmed_root.len == 0) "/" else trimmed_root;
    const base = if (std.mem.eql(u8, root, "/")) "" else root;
    var dirs: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, cgroup_text, '\n');
    while (it.next()) |raw_line| {
        const line = std.mem.trimEnd(u8, raw_line, "\r");
        var parts = std.mem.splitScalar(u8, line, ':');
        const hier = parts.next() orelse continue;
        const controllers = parts.next() orelse continue;
        _ = parts.peek() orelse continue;
        var path = parts.rest();
        if (path.len == 0 or path[0] != '/') {
            path = std.fmt.allocPrint(arena, "/{s}", .{path}) catch return &.{};
        }
        const rel = if (std.mem.eql(u8, path, "/")) "" else path;
        if (std.mem.eql(u8, hier, "0") and controllers.len == 0) {
            const full = std.fmt.allocPrint(arena, "{s}{s}", .{ base, rel }) catch return &.{};
            dirs.append(arena, if (full.len == 0) "/" else full) catch return &.{};
        } else if (hasCgroupController(controllers, "cpuset")) {
            dirs.append(arena, std.fmt.allocPrint(arena, "{s}/cpuset{s}", .{ base, rel }) catch return &.{}) catch return &.{};
            const full = std.fmt.allocPrint(arena, "{s}{s}", .{ base, rel }) catch return &.{};
            dirs.append(arena, if (full.len == 0) "/" else full) catch return &.{};
        }
    }
    return dirs.items;
}

fn cgroupDirectories(arena: Allocator, cgroup_text: []const u8) []const []const u8 {
    return cgroupDirectoriesFrom(arena, cgroup_text, "/sys/fs/cgroup");
}

const CgroupLine = struct {
    hier: []const u8,
    controllers: []const u8,
    path: []const u8,
};

fn parseCgroupLine(arena: Allocator, line: []const u8) ?CgroupLine {
    var parts = std.mem.splitScalar(u8, line, ':');
    const hier = parts.next() orelse return null;
    const controllers = parts.next() orelse return null;
    _ = parts.peek() orelse return null;
    var path = parts.rest();
    if (path.len == 0 or path[0] != '/') {
        path = std.fmt.allocPrint(arena, "/{s}", .{path}) catch return null;
    }
    return .{ .hier = hier, .controllers = controllers, .path = path };
}

const CgroupMount = struct {
    root: []const u8,
    point: []const u8,
    v2: bool,
    cpuset: bool,
};

/// Mirrors share/sitepath.py _unescape. The guard is one file and does not
/// import that script. mountinfo writes space, tab, newline and backslash
/// as \ooo.
fn unescapeMountinfo(arena: Allocator, raw: []const u8) ?[]const u8 {
    if (std.mem.indexOfScalar(u8, raw, '\\') == null) return raw;
    var out: std.ArrayList(u8) = .empty;
    var i: usize = 0;
    while (i < raw.len) {
        if (raw[i] == '\\' and i + 4 <= raw.len) {
            if (octalByte(raw[i + 1 .. i + 4])) |value| {
                out.append(arena, value) catch return null;
                i += 4;
                continue;
            }
        }
        out.append(arena, raw[i]) catch return null;
        i += 1;
    }
    return out.items;
}

fn octalByte(digits: []const u8) ?u8 {
    if (digits.len != 3) return null;
    var value: u16 = 0;
    for (digits) |ch| {
        if (ch < '0' or ch > '7') return null;
        value = value * 8 + (ch - '0');
    }
    if (value > 255) return null;
    return @intCast(value);
}

fn absMountPath(path: []const u8) bool {
    if (path.len == 0 or path[0] != '/') return false;
    var it = std.mem.splitScalar(u8, path, '/');
    while (it.next()) |part| if (std.mem.eql(u8, part, "..")) return false;
    return true;
}

fn trimMount(path: []const u8) []const u8 {
    if (std.mem.eql(u8, path, "/")) return path;
    const trimmed = std.mem.trimEnd(u8, path, "/");
    return if (trimmed.len == 0) "/" else trimmed;
}

fn controllersAllow(arena: Allocator, directory: []const u8) bool {
    const dir = if (std.mem.eql(u8, directory, "/")) "" else directory;
    const path = std.fmt.allocPrint(arena, "{s}/cgroup.controllers", .{dir}) catch return false;
    const raw = readFile(arena, path, 65536) orelse return true;
    var it = std.mem.tokenizeAny(u8, raw, " \t\r\n");
    while (it.next()) |tok| if (std.mem.eql(u8, tok, "cpuset")) return true;
    return false;
}

fn mountRel(mount_root: []const u8, cgroup_path: []const u8) ?[]const u8 {
    const root = trimMount(mount_root);
    if (std.mem.eql(u8, root, "/")) return if (std.mem.eql(u8, cgroup_path, "/")) "" else cgroup_path;
    if (std.mem.eql(u8, cgroup_path, root)) return "";
    if (cgroup_path.len > root.len and std.mem.startsWith(u8, cgroup_path, root) and cgroup_path[root.len] == '/') {
        return cgroup_path[root.len..];
    }
    return null;
}

fn joinMount(arena: Allocator, point: []const u8, rel: []const u8) ?[]const u8 {
    const base = trimMount(point);
    if (rel.len == 0) return base;
    if (std.mem.eql(u8, base, "/")) {
        if (rel[0] == '/') return rel;
        return std.fmt.allocPrint(arena, "/{s}", .{rel}) catch null;
    }
    if (rel[0] == '/') return std.fmt.allocPrint(arena, "{s}{s}", .{ base, rel }) catch null;
    return std.fmt.allocPrint(arena, "{s}/{s}", .{ base, rel }) catch null;
}

fn parseCgroupMounts(arena: Allocator, mount_text: []const u8) ?[]const CgroupMount {
    var mounts: std.ArrayList(CgroupMount) = .empty;
    var it = std.mem.splitScalar(u8, mount_text, '\n');
    while (it.next()) |raw_line| {
        const line = std.mem.trimEnd(u8, raw_line, "\r");
        const cut = std.mem.indexOf(u8, line, " - ") orelse continue;
        const head = line[0..cut];
        const tail = line[cut + 3 ..];
        var head_it = std.mem.tokenizeScalar(u8, head, ' ');
        var n: usize = 0;
        var root_field: []const u8 = "";
        var point_field: []const u8 = "";
        while (head_it.next()) |field| {
            if (n == 3) root_field = field;
            if (n == 4) point_field = field;
            n += 1;
        }
        if (n < 6) continue;
        var tail_it = std.mem.tokenizeScalar(u8, tail, ' ');
        const fstype = tail_it.next() orelse continue;
        if (!std.mem.eql(u8, fstype, "cgroup") and !std.mem.eql(u8, fstype, "cgroup2")) continue;
        _ = tail_it.next();
        const opts = tail_it.next() orelse "";
        const root = trimMount(unescapeMountinfo(arena, root_field) orelse return null);
        const point = trimMount(unescapeMountinfo(arena, point_field) orelse return null);
        mounts.append(arena, .{
            .root = root,
            .point = point,
            .v2 = std.mem.eql(u8, fstype, "cgroup2"),
            .cpuset = hasCgroupController(opts, "cpuset"),
        }) catch return null;
    }
    return mounts.items;
}

const MountHit = struct { set: ?CpuSet = null, dotdot: bool = false };

fn tryCgroupMounts(
    arena: Allocator,
    mounts: []const CgroupMount,
    v2: bool,
    cgroup_path: []const u8,
) ?MountHit {
    var matches: std.ArrayList(usize) = .empty;
    for (mounts, 0..) |mount, index| {
        if (mount.v2 != v2) continue;
        if (!v2 and !mount.cpuset) continue;
        if (mountRel(mount.root, cgroup_path) == null) continue;
        matches.append(arena, index) catch return null;
    }
    var cursor: usize = 1;
    while (cursor < matches.items.len) : (cursor += 1) {
        var slot = cursor;
        while (slot > 0 and mounts[matches.items[slot]].root.len > mounts[matches.items[slot - 1]].root.len) : (slot -= 1) {
            const tmp = matches.items[slot - 1];
            matches.items[slot - 1] = matches.items[slot];
            matches.items[slot] = tmp;
        }
    }
    var hit: MountHit = .{};
    var usable = false;
    for (matches.items) |index| {
        const mount = mounts[index];
        if (!absMountPath(mount.root) or !absMountPath(mount.point)) {
            hit.dotdot = true;
            continue;
        }
        usable = true;
        const rel = mountRel(mount.root, cgroup_path) orelse continue;
        const directory = joinMount(arena, mount.point, rel) orelse return null;
        if (walkCpuset(arena, directory, trimMount(mount.point))) |set| {
            hit.set = set;
            return hit;
        }
    }
    if (usable) hit.dotdot = false;
    return hit;
}

/// The cgroup v2 boundary: at most 64 parents, and the directory stays
/// equal to `root` or inside `root` + "/". `root` "/" allows any absolute path.
fn walkCpuset(arena: Allocator, start: []const u8, root: []const u8) ?CpuSet {
    var directory = start;
    var hop: usize = 0;
    while (hop < 64 and (std.mem.eql(u8, directory, root) or std.mem.startsWith(u8, directory, root) and (std.mem.eql(u8, root, "/") or directory[root.len] == '/'))) : (hop += 1) {
        const eff_dir = if (std.mem.eql(u8, directory, "/")) "" else directory;
        const eff = std.fmt.allocPrint(arena, "{s}/cpuset.cpus.effective", .{eff_dir}) catch return null;
        if (readFile(arena, eff, 65536)) |raw| {
            const trimmed = std.mem.trim(u8, raw, " \t\r\n");
            if (trimmed.len > 0 and controllersAllow(arena, directory)) {
                if (parseCpuSpec(trimmed)) |set| return set;
            }
        }
        const parent = std.fs.path.dirname(directory) orelse break;
        if (std.mem.eql(u8, parent, directory)) break;
        directory = parent;
    }
    return null;
}

fn cgroupCpusLegacy(arena: Allocator, cgroup_text: []const u8, root: []const u8) ?CpuSet {
    for (cgroupDirectoriesFrom(arena, cgroup_text, root)) |start| {
        if (walkCpuset(arena, start, root)) |set| return set;
    }
    return null;
}

fn mountinfoBeside(arena: Allocator, proc_cgroup_path: []const u8) ?[]const u8 {
    const dir = std.fs.path.dirname(proc_cgroup_path) orelse ".";
    return std.fmt.allocPrint(arena, "{s}/mountinfo", .{dir}) catch null;
}

fn cgroupCpusMounted(
    arena: Allocator,
    root: []const u8,
    cgroup_text: []const u8,
    mount_text: []const u8,
) ?CpuSet {
    const mounts = parseCgroupMounts(arena, mount_text) orelse return null;
    var dotdot = false;
    var it = std.mem.splitScalar(u8, cgroup_text, '\n');
    while (it.next()) |raw_line| {
        const parsed = parseCgroupLine(arena, std.mem.trimEnd(u8, raw_line, "\r")) orelse continue;
        const v2 = std.mem.eql(u8, parsed.hier, "0") and parsed.controllers.len == 0;
        if (!v2 and !hasCgroupController(parsed.controllers, "cpuset")) continue;
        const hit = tryCgroupMounts(arena, mounts, v2, parsed.path) orelse return null;
        if (hit.set) |set| return set;
        if (hit.dotdot) dotdot = true;
    }
    if (dotdot) return cgroupCpusLegacy(arena, cgroup_text, root);
    return null;
}

fn cgroupCpusFrom(arena: Allocator, proc_cgroup_path: []const u8, cgroup_root: []const u8) ?CpuSet {
    const trimmed_root = std.mem.trimEnd(u8, cgroup_root, "/");
    const root = if (trimmed_root.len == 0) "/" else trimmed_root;
    const cgroup_text = readFile(arena, proc_cgroup_path, 1 << 20) orelse return null;
    const mount_path = mountinfoBeside(arena, proc_cgroup_path) orelse
        return cgroupCpusLegacy(arena, cgroup_text, root);
    const mount_text = readFile(arena, mount_path, 1 << 20) orelse
        return cgroupCpusLegacy(arena, cgroup_text, root);
    return cgroupCpusMounted(arena, root, cgroup_text, mount_text);
}

fn cgroupCpus(arena: Allocator) ?CpuSet {
    return cgroupCpusFrom(arena, "/proc/self/cgroup", "/sys/fs/cgroup");
}

test "cgroupDirectories preserves colons in paths and requires exact cpuset controller" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const dirs = cgroupDirectories(a, "2:mycpuset:/ignored\n0::/system.slice/job:42\n");
    try std.testing.expectEqual(@as(usize, 1), dirs.len);
    try std.testing.expectEqualStrings("/sys/fs/cgroup/system.slice/job:42", dirs[0]);
}

fn schedCpus() ?CpuSet {
    var buf: [cpu_id_max / 8]u8 = std.mem.zeroes([cpu_id_max / 8]u8);
    const rc = linux.syscall3(.sched_getaffinity, 0, buf.len, @intFromPtr(&buf));
    const signed: isize = @bitCast(rc);
    if (signed < 0) return null;
    const n: usize = @min(@as(usize, @intCast(signed)), buf.len);
    var set: CpuSet = .{};
    var i: usize = 0;
    while (i < n) : (i += 1) {
        var b: u3 = 0;
        while (true) : (b += 1) {
            if (buf[i] & (@as(u8, 1) << b) != 0) set.add(i * 8 + b);
            if (b == 7) break;
        }
    }
    return set;
}

fn bindCpus(env: Map) ?CpuSet {
    const listed = std.mem.trim(u8, env.get("SLURM_CPU_BIND_LIST") orelse "", " \t\r\n");
    if (listed.len > 0) return parseCpuSpec(listed);
    const verbose = std.mem.trim(u8, env.get("SLURM_CPU_BIND") orelse "", " \t\r\n");
    const colon = std.mem.lastIndexOfScalar(u8, verbose, ':') orelse return null;
    return parseCpuSpec(std.mem.trim(u8, verbose[colon + 1 ..], " \t\r\n"));
}

const TaskCpuBounds = struct { allowed: ?CpuSet, base_cpus: ?CpuSet };

fn taskCpuBounds(env: Map, affinity: ?CpuSet, cgroup: ?CpuSet) TaskCpuBounds {
    const base_cpus: ?CpuSet = cgroup orelse affinity;
    const bind = bindCpus(env);
    var allowed = affinity orelse cgroup orelse bind orelse return .{ .allowed = null, .base_cpus = null };
    if (affinity) |a| allowed = allowed.bitAnd(a);
    if (cgroup) |cg| allowed = allowed.bitAnd(cg);
    if (bind) |bd| allowed = allowed.bitAnd(bd);
    return .{ .allowed = allowed, .base_cpus = base_cpus };
}

/// task_cpu_ids: the intersection of affinity, cpuset, and the Slurm bind list.
fn taskCpuIds(arena: Allocator, env: Map) ?CpuSet {
    return taskCpuBounds(env, schedCpus(), cgroupCpus(arena)).allowed;
}

fn normKey(arena: Allocator, key: []const u8) []const u8 {
    const buf = arena.alloc(u8, key.len) catch return key;
    var n: usize = 0;
    var pending = false;
    for (key) |ch| {
        if (ch == ' ' or ch == '\t') {
            pending = n > 0;
            continue;
        }
        if (pending) {
            buf[n] = '_';
            n += 1;
            pending = false;
        }
        buf[n] = std.ascii.toLower(ch);
        n += 1;
    }
    return buf[0..n];
}

const ProcessorId = struct { id: ?usize };

fn processorId(line: []const u8) ?ProcessorId {
    const t = std.mem.trim(u8, line, " \t\r");
    const prefix = "processor";
    if (t.len < prefix.len or !std.ascii.eqlIgnoreCase(t[0..prefix.len], prefix)) return null;
    var i: usize = prefix.len;
    while (i < t.len and (t[i] == ' ' or t[i] == '\t')) i += 1;
    if (i >= t.len or t[i] != ':') return null;
    i += 1;
    while (i < t.len and (t[i] == ' ' or t[i] == '\t')) i += 1;
    if (i >= t.len) return null;
    for (t[i..]) |c| if (c < '0' or c > '9') return null;
    return .{ .id = parseUsize(t[i..]) };
}

const CpuField = struct { norm: []const u8, raw: []const u8, value: []const u8 };

fn cpuField(arena: Allocator, line: []const u8) ?CpuField {
    const trimmed = std.mem.trim(u8, line, " \t\r");
    const colon = std.mem.indexOfScalar(u8, trimmed, ':') orelse return null;
    const raw = std.mem.trim(u8, trimmed[0..colon], " \t");
    if (raw.len == 0) return null;
    return .{
        .norm = normKey(arena, raw),
        .raw = raw,
        .value = std.mem.trim(u8, trimmed[colon + 1 ..], " \t"),
    };
}

fn isFlagKey(norm: []const u8) bool {
    return std.mem.eql(u8, norm, "flags") or std.mem.eql(u8, norm, "features") or std.mem.eql(u8, norm, "isa");
}

fn fieldValue(fields: []const CpuField, norm: []const u8) ?[]const u8 {
    for (fields) |f| if (std.mem.eql(u8, f.norm, norm)) return f.value;
    return null;
}

fn sameFields(a: []const CpuField, b: []const CpuField) bool {
    for (a) |f| {
        const first_a = fieldValue(a, f.norm) orelse continue;
        if (!std.mem.eql(u8, f.value, first_a)) continue;
        const first_b = fieldValue(b, f.norm) orelse return false;
        if (!std.mem.eql(u8, first_a, first_b)) return false;
    }
    for (b) |f| {
        if (fieldValue(a, f.norm) == null) return false;
    }
    return true;
}

fn tokenSet(arena: Allocator, value: []const u8) std.BufSet {
    var set = std.BufSet.init(arena);
    var it = std.mem.tokenizeAny(u8, value, " \t");
    while (it.next()) |tok| set.insert(tok) catch {};
    return set;
}

const CpuBlock = struct { id: ?usize, fields: []const CpuField };

fn resolveLxcfsLogicalIds(arena: Allocator, blocks: []const CpuBlock, base_cpus: ?CpuSet) ?[]?usize {
    const out = arena.alloc(?usize, blocks.len) catch return null;
    var raw_set: CpuSet = .{};
    var seq = true;
    for (blocks, 0..) |b, idx| {
        out[idx] = b.id;
        const id = b.id orelse {
            seq = false;
            continue;
        };
        if (id >= cpu_id_max or id != idx) {
            seq = false;
        } else raw_set.add(id);
    }
    if (base_cpus) |bc| {
        if (seq and blocks.len > 0 and blocks.len == bc.count() and !raw_set.eql(bc)) {
            const ids = bc.sortedIds(arena) catch return null;
            if (ids.len == blocks.len) for (ids, 0..) |lid, idx| {
                out[idx] = lid;
            };
        }
    }
    return out;
}

/// scope_cpuinfo: null when the text is already the allowed processors.
fn scopeCpuinfoWithBase(arena: Allocator, cpuinfo: []const u8, allowed: CpuSet, base_cpus: ?CpuSet) ?[]const u8 {
    var blocks: std.ArrayList(CpuBlock) = .empty;
    var cur_id: ?ProcessorId = null;
    var cur: std.ArrayList(CpuField) = .empty;
    var it = std.mem.splitScalar(u8, cpuinfo, '\n');
    while (it.next()) |line| {
        if (processorId(line)) |pid| {
            if (cur_id) |prev| blocks.append(arena, .{ .id = prev.id, .fields = cur.toOwnedSlice(arena) catch return null }) catch return null;
            cur = .empty;
            cur_id = pid;
            continue;
        }
        if (cur_id == null) continue;
        if (cpuField(arena, line)) |f| {
            if (!std.mem.eql(u8, f.norm, "processor")) cur.append(arena, f) catch return null;
        }
    }
    if (cur_id) |prev| blocks.append(arena, .{ .id = prev.id, .fields = cur.toOwnedSlice(arena) catch return null }) catch return null;
    if (blocks.items.len == 0) return null;
    const logical_ids = resolveLxcfsLogicalIds(arena, blocks.items, base_cpus) orelse return null;
    var kept: std.ArrayList(CpuBlock) = .empty;
    for (blocks.items, logical_ids) |b, maybe_lid| if (maybe_lid) |lid| if (allowed.has(lid)) kept.append(arena, b) catch return null;
    if (kept.items.len == blocks.items.len and kept.items.len > 0) {
        const base = kept.items[0].fields;
        var same = true;
        for (kept.items[1..]) |b| if (!sameFields(base, b.fields)) {
            same = false;
            break;
        };
        if (same) return null;
    }
    if (kept.items.len == 0) return arena.dupe(u8, "processor : 0\nflags :\n") catch null;
    const first = kept.items[0];
    var out: std.ArrayList(u8) = .empty;
    out.print(arena, "processor : {d}\n", .{first.id.?}) catch return null;
    var seen: std.BufSet = .init(arena);
    for (first.fields) |f| {
        if (seen.contains(f.norm)) continue;
        seen.insert(f.norm) catch return null;
        if (isFlagKey(f.norm)) {
            var shared: ?std.BufSet = null;
            for (kept.items) |b| {
                const value = fieldValue(b.fields, f.norm) orelse "";
                var tokens = tokenSet(arena, value);
                if (shared) |*got| {
                    var next = std.BufSet.init(arena);
                    var kit = got.iterator();
                    while (kit.next()) |tok| if (tokens.contains(tok.*)) next.insert(tok.*) catch return null;
                    shared = next;
                } else shared = tokens;
            }
            out.print(arena, "{s} :", .{f.raw}) catch return null;
            if (shared) |got| {
                var names: std.ArrayList([]const u8) = .empty;
                var kit = got.iterator();
                while (kit.next()) |tok| names.append(arena, tok.*) catch return null;
                std.mem.sort([]const u8, names.items, {}, struct {
                    fn less(_: void, a: []const u8, b: []const u8) bool {
                        return std.mem.lessThan(u8, a, b);
                    }
                }.less);
                for (names.items) |name| out.print(arena, " {s}", .{name}) catch return null;
            }
            out.append(arena, '\n') catch return null;
            continue;
        }
        const value = fieldValue(first.fields, f.norm) orelse continue;
        var agree = true;
        for (kept.items) |b| {
            const other = fieldValue(b.fields, f.norm) orelse {
                agree = false;
                break;
            };
            if (!std.mem.eql(u8, other, value)) {
                agree = false;
                break;
            }
        }
        if (!agree) continue;
        out.print(arena, "{s} : {s}\n", .{ f.raw, value }) catch return null;
    }
    return out.toOwnedSlice(arena) catch null;
}

fn scopeCpuinfo(arena: Allocator, cpuinfo: []const u8, allowed: CpuSet) ?[]const u8 {
    return scopeCpuinfoWithBase(arena, cpuinfo, allowed, null);
}

fn unlinkPath(path: []const u8) void {
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, path) catch return;
    _ = linux.unlinkat(linux.AT.FDCWD, z.ptr, 0);
}

fn tempCpuFile(c: *Ctx, env: Map, data: []const u8) ?[]const u8 {
    const base = pyTempDir(c, env);
    const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_";
    var tries: usize = 0;
    while (tries < 100) : (tries += 1) {
        var rnd: [8]u8 = undefined;
        _ = sys.call(linux.getrandom, .{ &rnd, rnd.len, 0 }) catch return null;
        var suffix: [8]u8 = undefined;
        for (rnd, 0..) |b, i| suffix[i] = alphabet[b % alphabet.len];
        const name = c.arena.print("eessi-arch-guard-cpu-{s}", .{suffix}) catch return null;
        const p = std.fs.path.join(c.arena, &.{ base, name }) catch return null;
        if (writePrivateNew(p, data, 0o600)) return p;
    }
    return null;
}

const PreparedCpu = struct { path: []const u8, cleanup: ?[]const u8 = null, child: Map };

fn prepareCpuinfoWithTemp(c: *Ctx, env: Map, temp_env: Map) PreparedCpu {
    const explicit = env.get("EESSI_PROC_CPUINFO") orelse "";
    const path: []const u8 = if (explicit.len > 0) explicit else "/proc/cpuinfo";
    const bounds = taskCpuBounds(env, schedCpus(), cgroupCpus(c.arena));
    const allowed = bounds.allowed orelse return .{ .path = path, .child = env };
    const blob = readFile(c.arena, path, cpuinfo_read_max) orelse return .{ .path = path, .child = env };
    const scoped = scopeCpuinfoWithBase(c.arena, blob, allowed, bounds.base_cpus) orelse return .{ .path = path, .child = env };
    const tmp = tempCpuFile(c, temp_env, scoped) orelse return .{ .path = path, .child = env };
    var child = envmap.clone(c.arena, env) catch {
        unlinkPath(tmp);
        return .{ .path = path, .child = env };
    };
    child.put(c.arena, "EESSI_PROC_CPUINFO", tmp) catch {
        unlinkPath(tmp);
        return .{ .path = path, .child = env };
    };
    return .{ .path = tmp, .cleanup = tmp, .child = child };
}

fn prepareCpuinfo(c: *Ctx, env: Map) PreparedCpu {
    return prepareCpuinfoWithTemp(c, env, env);
}

/// probe_cpu: this node's CPU target. Runs `eessi_archdetect.sh -a cpupath`
/// from EESSI_INIT_PREFIX on the processors the task may use, drops the
/// candidates whose -march needs waitpkg when a kept processor lacks it, and
/// takes the first candidate with a directory
/// `$EESSI_PREFIX/software/$EESSI_OS_TYPE/<candidate>/software`.
pub fn probeCpu(c: *Ctx, env: Map) Stop!CpuProbe {
    const prefix = env.get("EESSI_PREFIX") orelse empty;
    const os_type = env.get("EESSI_OS_TYPE") orelse empty;
    const init = env.get("EESSI_INIT_PREFIX") orelse empty;
    if (!text.safeAbsolute(prefix) or !text.safeRelSubdir(os_type) or !text.safeAbsolute(init))
        return .{ .status = .@"error", .err = "EESSI_PREFIX, EESSI_OS_TYPE, or EESSI_INIT_PREFIX is missing or unsafe" };
    const script = std.fs.path.join(c.arena, &.{ init, "eessi_archdetect.sh" }) catch return .{ .status = .@"error", .err = "out of memory" };
    if (!isFile(script)) return .{ .status = .@"error", .err = "eessi_archdetect.sh is not available" };
    const prepared = prepareCpuinfo(c, env);
    defer if (prepared.cleanup) |p| unlinkPath(p);
    var why: []const u8 = "";
    const r = runBash(c, &.{ script, "-a", "cpupath" }, prepared.child, &why) catch |err| switch (err) {
        error.Deadline, error.Terminated => |e| return e,
        else => return .{ .status = .@"error", .err = c.arena.print("could not run archdetect: {s}", .{why}) catch "could not run archdetect" },
    };
    if (r.code != 0) {
        const detail = firstLine(decodeReplace(c, r.stderr)) orelse (c.arena.print("exit {d}", .{exitShown(r.code)}) catch "exit");
        return .{ .status = .@"error", .err = line1(c, detail, 200) };
    }
    const parsed = (parseCpuLine(c.arena, decodeReplace(c, r.stdout)) catch null) orelse
        return .{ .status = .@"error", .err = "cpupath output is not a single line" };
    var cands: std.ArrayList([]const u8) = .empty;
    for (parsed) |p| if (p.len > 0) cands.append(c.arena, p) catch main_mod.oom();
    if (cands.items.len == 0) return .{ .status = .@"error", .err = "cpupath returned no candidates" };
    for (cands.items) |cand| if (!text.safeRelSubdir(cand))
        return .{ .status = .@"error", .candidates = cands.items, .err = "cpupath returned an unsafe candidate" };
    const flags = readCpuFlags(c.arena, prepared.path);
    var compatible: std.ArrayList([]const u8) = .empty;
    for (cands.items) |cand| if (candidateSupportsCpu(cand, flags)) compatible.append(c.arena, cand) catch main_mod.oom();
    const chosen = installed(c, prefix, os_type, compatible.items);
    const raw = installed(c, prefix, os_type, cands.items);
    const override = if (chosen.len > 0 and !std.mem.eql(u8, chosen, raw)) chosen else "";
    if (chosen.len > 0) return .{ .status = .ok, .target = chosen, .candidates = compatible.items, .override = override };
    return .{ .status = .none, .candidates = compatible.items };
}

/// A signal-killed child shows as a negative return code in Python.
fn exitShown(code: u32) i64 {
    return if (code > 128 and code < 128 + 65) -@as(i64, code - 128) else code;
}

fn installed(c: *Ctx, prefix: []const u8, os_type: []const u8, items: []const []const u8) []const u8 {
    for (items) |item| {
        const d = std.fs.path.join(c.arena, &.{ prefix, "software", os_type, item, "software" }) catch continue;
        if (isDir(d)) return item;
    }
    return "";
}

/// probe_accel: this node's accelerator subdirectory from
/// `eessi_archdetect.sh accelpath`. A nonzero exit or empty output is no
/// accelerator; other output must be one accel/nvidia/ccN or accel/amd/gfxN.
pub fn probeAccel(c: *Ctx, env: Map) Stop!AccelProbe {
    const init = env.get("EESSI_INIT_PREFIX") orelse empty;
    if (!text.safeAbsolute(init)) return .{ .status = .@"error", .err = "EESSI_INIT_PREFIX is missing or unsafe" };
    const script = std.fs.path.join(c.arena, &.{ init, "eessi_archdetect.sh" }) catch return .{ .status = .@"error", .err = "out of memory" };
    if (!isFile(script)) return .{ .status = .@"error", .err = "eessi_archdetect.sh is not available" };
    var why: []const u8 = "";
    const r = runBash(c, &.{ script, "accelpath" }, env, &why) catch |err| switch (err) {
        error.Deadline, error.Terminated => |e| return e,
        else => return .{ .status = .@"error", .err = c.arena.print("could not run archdetect: {s}", .{why}) catch "could not run archdetect" },
    };
    if (r.code != 0) return .{ .status = .none };
    const out = text.pyStrip(decodeReplace(c, r.stdout));
    if (out.len == 0) return .{ .status = .none };
    if (std.mem.findAny(u8, out, "\n\r") != null or !text.accelSubdir(out))
        return .{ .status = .@"error", .err = "accelpath output is not an accelerator subdirectory" };
    return .{ .status = .ok, .subdir = out };
}

/// lmod_invocable: whether LMOD_CMD can be run as given: not empty, not an
/// option, no line break or NUL, and a regular file this user may execute.
pub fn lmodInvocable(path: []const u8) bool {
    if (path.len == 0 or path[0] == '-' or text.hasLineBreakOrNul(path)) return false;
    if (!isFile(path)) return false;
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, path) catch return false;
    return linux.errno(linux.faccessat(linux.AT.FDCWD, z.ptr, linux.X_OK, 0)) == .SUCCESS;
}

// ---- workspaces --------------------------------------------------------

/// _private_directory: an existing directory, not a symbolic link, owned by
/// this user with no group or other bits; its resolved path.
fn privateDirectory(c: *Ctx, path: []const u8) ?[]const u8 {
    if (!text.safeAbsolute(path)) return null;
    const fd = fsx.openDir(fsx.cwd, path, false) catch return null;
    defer sys.close(fd);
    const st = fsx.fstat(fd) catch return null;
    if (st.uid != sys.getuid() or st.mode & 0o077 != 0) return null;
    var rb: [sys.path_max]u8 = undefined;
    const real = fsx.realpathFd(fd, &rb) catch return null;
    return c.arena.dupe(u8, real) catch null;
}

/// No directory the restore may work in; the reason is in `why`.
pub const WorkError = error{Unavailable};

/// restore_work_base: EESSI_ARCH_GUARD_WORKDIR when set (must be private),
/// else a private TMPDIR, else in strict mode the private state directory,
/// else TMPDIR as it is, else the system temporary directory.
pub fn restoreWorkBase(c: *Ctx, env: Map, why: *[]const u8) WorkError![]const u8 {
    const explicit = env.get("EESSI_ARCH_GUARD_WORKDIR") orelse empty;
    if (explicit.len > 0) {
        if (privateDirectory(c, explicit)) |d| return d;
        why.* = "EESSI_ARCH_GUARD_WORKDIR is not an existing private owned directory";
        return error.Unavailable;
    }
    const temporary = env.get("TMPDIR") orelse empty;
    if (privateDirectory(c, temporary)) |d| return d;
    if (c.strict) {
        var st_dir = env.get("XDG_STATE_HOME") orelse empty;
        if (st_dir.len == 0) st_dir = std.fs.path.join(c.arena, &.{ env.get("HOME") orelse empty, ".local", "state" }) catch "";
        const p = std.fs.path.join(c.arena, &.{ st_dir, "eessi-arch-guard" }) catch "";
        if (privateDirectory(c, p)) |d| return d;
        why.* = "strict mode needs an existing private job or state workspace";
        return error.Unavailable;
    }
    if (text.safeAbsolute(temporary) and isDir(temporary)) return temporary;
    return pyTempDir(c, env);
}

/// tempfile.gettempdir(): TMPDIR, TEMP, TMP, then /tmp, /var/tmp, /usr/tmp,
/// then the current directory; the first that is a writable directory.
fn pyTempDir(c: *Ctx, env: Map) []const u8 {
    for ([_][]const u8{ "TMPDIR", "TEMP", "TMP" }) |k| {
        const v = env.get(k) orelse continue;
        if (v.len > 0 and writableDir(v)) return absolutize(c, v);
    }
    for ([_][]const u8{ "/tmp", "/var/tmp", "/usr/tmp" }) |d| if (writableDir(d)) return d;
    return absolutize(c, ".");
}

fn absolutize(c: *Ctx, p: []const u8) []const u8 {
    if (p.len > 0 and p[0] == '/') return p;
    var rb: [sys.path_max]u8 = undefined;
    const cwd = fsx.realpath(fsx.cwd, ".", &rb) catch return p;
    return std.fs.path.join(c.arena, &.{ cwd, p }) catch p;
}

fn writableDir(p: []const u8) bool {
    if (!isDir(p)) return false;
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, p) catch return false;
    return linux.errno(linux.faccessat(linux.AT.FDCWD, z.ptr, linux.W_OK | linux.X_OK, 0)) == .SUCCESS;
}

/// tempfile.mkdtemp(prefix="eessi-arch-guard-", dir=base): a new 0700
/// directory with a random suffix.
fn mkdtemp(c: *Ctx, base: []const u8) ?[]const u8 {
    return fsx.mkdtemp(c.arena, base, "eessi-arch-guard-") catch null;
}

fn rmtree(c: *Ctx, p: []const u8) void {
    fsx.deleteTree(c.arena, fsx.cwd, p) catch {};
}

// ---- the restore helper, caller side -------------------------------------

/// RestoreResult. `ran` is false when the restore stopped before Lmod could
/// answer: no workspace, no helper, or no usable reply from it.
pub const Restore = struct {
    /// The helper loaded every module.
    ok: bool,
    /// The environment after the loads, when `ok`.
    env: ?Map = null,
    /// The modules the helper names as not loading for this node.
    missing: []const []const u8 = &.{},
    /// The first line of the helper's error, or why the restore did not run.
    err: []const u8 = "",
    ran: bool = true,
};

fn notRun(err: []const u8) Restore {
    return .{ .ok = false, .err = err, .ran = false };
}

fn wireMap(c: *Ctx, out: *std.ArrayList(u8), m: Map) Allocator.Error!void {
    try out.append(c.arena, '{');
    var it = m.iterator();
    var first = true;
    while (it.next()) |kv| {
        if (!first) try out.appendSlice(c.arena, ", ");
        first = false;
        try json.writeWireString(out, c.arena, kv.key_ptr.*);
        try out.appendSlice(c.arena, ": ");
        try json.writeWireString(out, c.arena, kv.value_ptr.*);
    }
    try out.append(c.arena, '}');
}

fn callRestore(c: *Ctx, env: Map, eessi: []const u8, modules: []const []const u8, pin: []const u8) Stop!Restore {
    const a = c.arena;
    var req: std.ArrayList(u8) = .empty;
    buildRequest(c, &req, env, eessi, modules, pin) catch return notRun("out of memory");
    var why: []const u8 = "";
    const base = restoreWorkBase(c, env, &why) catch
        return notRun(a.print("restore workspace unavailable: {s}", .{why}) catch "restore workspace unavailable");
    const work = mkdtemp(c, base) orelse return notRun("restore workspace unavailable: could not create a directory");
    defer rmtree(c, work);
    var clean: Map = .empty;
    clean.put(a, "PATH", "/usr/bin:/bin") catch main_mod.oom();
    clean.put(a, "LC_ALL", "C") catch main_mod.oom();
    clean.put(a, "LANG", "C") catch main_mod.oom();
    clean.put(a, "HOME", work) catch main_mod.oom();
    clean.put(a, "TMPDIR", work) catch main_mod.oom();
    clean.put(a, "EESSI_ARCH_GUARD_WORKDIR", work) catch main_mod.oom();
    var argv: std.ArrayList([]const u8) = .empty;
    argv.appendSlice(a, &.{ c.self_exe, "--restore" }) catch main_mod.oom();
    if (c.strict) argv.append(a, "--strict") catch main_mod.oom();
    const r = runCaptured(c, argv.items, clean, req.items, c.limit != null, &why) catch |err| switch (err) {
        error.Deadline, error.Terminated => |e| return e,
        else => return notRun(a.print("could not start restore helper: {s}", .{why}) catch "could not start restore helper"),
    };
    if (r.code != 0 or r.stdout.len == 0) {
        const e = decodeReplace(c, r.stderr);
        return notRun(line1(c, if (e.len > 0) e else "restore helper failed", 300));
    }
    const data = json.parse(a, r.stdout) catch return notRun("restore helper returned invalid output");
    var missing: std.ArrayList([]const u8) = .empty;
    if (data.get("missing")) |m| if (m == .array) for (m.array) |item| {
        if (item != .string) continue;
        const name = json.decodeCache(a, item.string) catch continue;
        if (text.moduleName(name)) missing.append(a, name) catch main_mod.oom();
    };
    const ok = if (data.get("ok")) |v| v == .bool and v.bool else false;
    if (!ok) {
        const e: []const u8 = if (data.get("error")) |v| (if (v == .string) json.decodeCache(a, v.string) catch "" else "") else "";
        return .{ .ok = false, .missing = missing.items, .err = line1(c, e, 300) };
    }
    const no_env = "restore helper returned no environment";
    const restored = data.get("env") orelse return notRun(no_env);
    if (restored != .object) return notRun(no_env);
    var m: Map = .empty;
    for (restored.object) |mem| {
        const k = json.decodeWire(a, mem.key) catch return notRun(no_env);
        if (mem.value != .string) return notRun(no_env);
        const v = json.decodeWire(a, mem.value.string) catch return notRun(no_env);
        m.put(a, k, v) catch main_mod.oom();
    }
    return .{ .ok = true, .env = m, .missing = missing.items };
}

fn buildRequest(c: *Ctx, out: *std.ArrayList(u8), env: Map, eessi: []const u8, modules: []const []const u8, pin: []const u8) Allocator.Error!void {
    const a = c.arena;
    try out.appendSlice(a, "{\"v\": 1, \"env\": ");
    try wireMap(c, out, env);
    try out.appendSlice(a, ", \"eessi\": ");
    try json.writeWireString(out, a, eessi);
    try out.appendSlice(a, ", \"modules\": [");
    for (modules, 0..) |m, i| {
        if (i > 0) try out.appendSlice(a, ", ");
        try json.writeWireString(out, a, m);
    }
    try out.appendSlice(a, "], \"lmod\": ");
    try json.writeWireString(out, a, env.get("LMOD_CMD") orelse empty);
    try out.appendSlice(a, ", \"pin\": ");
    try json.writeWireString(out, a, pin);
    try out.append(a, '}');
}

// ---- the restore helper, helper side -------------------------------------

const apply_sh = "source \"$1\" >/dev/null || exit 4\nexec env -0\n";

const Applied = struct { env: ?Map, err: []const u8 };

fn writePrivateNew(path: []const u8, data: []const u8, mode: u32) bool {
    const fd = sys.openat(fsx.cwd, path, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true, .NOFOLLOW = true }, 0o600) catch return false;
    defer sys.close(fd);
    sys.writeAll(fd, data) catch return false;
    if (mode != 0o600) sys.fchmod(fd, mode) catch return false;
    return true;
}

fn parseEnv0(c: *Ctx, blob: []const u8) Map {
    var m: Map = .empty;
    var it = std.mem.splitScalar(u8, blob, 0);
    while (it.next()) |part| {
        const eq = std.mem.findScalar(u8, part, '=') orelse continue;
        m.put(c.arena, part[0..eq], part[eq + 1 ..]) catch main_mod.oom();
    }
    return m;
}

const HelperError = error{Raise};

fn sourceExports(c: *Ctx, module_env: Map, script: []const u8, raised: *[]const u8) HelperError!Applied {
    var why: []const u8 = "";
    const base = restoreWorkBase(c, c.env, &why) catch {
        raised.* = why;
        return error.Raise;
    };
    const tmp = mkdtemp(c, base) orelse {
        raised.* = "could not create a temporary directory";
        return error.Raise;
    };
    defer rmtree(c, tmp);
    const fragment = std.fs.path.join(c.arena, &.{ tmp, "fragment.sh" }) catch return error.Raise;
    const apply = std.fs.path.join(c.arena, &.{ tmp, "apply.sh" }) catch return error.Raise;
    if (!writePrivateNew(fragment, script, 0o600) or !writePrivateNew(apply, apply_sh, 0o700)) {
        raised.* = "could not write the restore scripts";
        return error.Raise;
    }
    const stripped = envmap.stripStartup(c.arena, module_env) catch return error.Raise;
    const r = runCaptured(c, &.{ "bash", "--noprofile", "--norc", "--", apply, fragment }, stripped, null, false, &why) catch {
        raised.* = why;
        return error.Raise;
    };
    if (r.code != 0) {
        const e = decodeReplace(c, r.stderr);
        return .{ .env = null, .err = if (e.len > 0) e else c.arena.print("apply exited {d}", .{exitShown(r.code)}) catch "apply failed" };
    }
    return .{ .env = parseEnv0(c, r.stdout), .err = "" };
}

fn applyLmod(c: *Ctx, module_env: Map, lmod: []const u8, args: []const []const u8, raised: *[]const u8) HelperError!Applied {
    var argv: std.ArrayList([]const u8) = .empty;
    argv.appendSlice(c.arena, &.{ lmod, "bash" }) catch return error.Raise;
    argv.appendSlice(c.arena, args) catch return error.Raise;
    const stripped = envmap.stripStartup(c.arena, module_env) catch return error.Raise;
    var why: []const u8 = "";
    const r = runCaptured(c, argv.items, stripped, null, false, &why) catch {
        raised.* = why;
        return error.Raise;
    };
    if (r.code != 0) return .{ .env = null, .err = decodeReplace(c, r.stderr) };
    return sourceExports(c, module_env, r.stdout, raised);
}

/// pinned_env: the environment for loading EESSI pinned to one CPU
/// subdirectory. A login-node EESSI_ARCHDETECT_OPTIONS would outrank the pin
/// inside the EESSI module; the purge before this load still needs it, as
/// the module's unload computes the tree to remove from it.
fn pinnedEnv(c: *Ctx, env: Map, pin: []const u8) Map {
    if (pin.len == 0) return env;
    var m = env.clone(c.arena) catch main_mod.oom();
    _ = m.orderedRemove("EESSI_ARCHDETECT_OPTIONS");
    m.put(c.arena, "EESSI_SOFTWARE_SUBDIR_OVERRIDE", pin) catch main_mod.oom();
    return m;
}

const InOrder = struct {
    env: Map,
    missing: []const []const u8,
    err: []const u8,
};

/// load_in_order: one Lmod call per module, in order, past any that fail.
/// One call finds every name before it loads any, so a module from a tree an
/// earlier module adds loads only in a later call. Stops at diagnose_limit
/// failures; the error is the first failure's.
fn loadInOrder(c: *Ctx, start: Map, lmod: []const u8, modules: []const []const u8, raised: *[]const u8) HelperError!InOrder {
    var current = start;
    var missing: std.ArrayList([]const u8) = .empty;
    var first_err: []const u8 = "";
    for (modules) |name| {
        const l = try applyLmod(c, current, lmod, &.{ "load", name }, raised);
        if (l.env) |next| {
            current = next;
            continue;
        }
        missing.append(c.arena, name) catch main_mod.oom();
        if (first_err.len == 0) first_err = l.err;
        if (missing.items.len == diagnose_limit) break;
    }
    return .{ .env = current, .missing = missing.items, .err = first_err };
}

const Reply = struct {
    ok: bool,
    env: ?Map = null,
    missing: []const []const u8 = &.{},
    err: []const u8 = "",
};

fn malformed() Reply {
    return .{ .ok = false, .err = "restore request is malformed" };
}

fn doRestore(c: *Ctx, payload: json.Value, raised: *[]const u8) HelperError!Reply {
    const a = c.arena;
    const env_v = payload.get("env") orelse return malformed();
    const eessi_v = payload.get("eessi") orelse return malformed();
    const modules_v = payload.get("modules") orelse return malformed();
    const lmod_v = payload.get("lmod") orelse return malformed();
    if (env_v != .object or eessi_v != .string or modules_v != .array or lmod_v != .string) return malformed();
    const eessi = json.decodeWire(a, eessi_v.string) catch return malformed();
    const lmod = json.decodeWire(a, lmod_v.string) catch return malformed();
    var pin: []const u8 = "";
    if (payload.get("pin")) |pin_v| {
        if (pin_v != .string) return malformed();
        pin = json.decodeWire(a, pin_v.string) catch return malformed();
    }
    var modules: std.ArrayList([]const u8) = .empty;
    var module_bad = false;
    for (modules_v.array) |m| {
        if (m != .string) {
            module_bad = true;
            continue;
        }
        modules.append(a, json.decodeWire(a, m.string) catch return malformed()) catch main_mod.oom();
    }
    var module_env: Map = .empty;
    for (env_v.object) |mem| {
        if (mem.value != .string) return malformed();
        const k = json.decodeWire(a, mem.key) catch return malformed();
        const v = json.decodeWire(a, mem.value.string) catch return malformed();
        module_env.put(a, k, v) catch main_mod.oom();
    }
    if (pin.len > 0 and !text.safeRelSubdir(pin)) return malformed();
    if (!lmodInvocable(lmod)) return .{ .ok = false, .err = "LMOD_CMD is not executable" };
    if (!text.moduleName(eessi) or !text.eessiModule(eessi)) return .{ .ok = false, .err = "unsafe-module-name" };
    if (module_bad) return .{ .ok = false, .err = "unsafe-module-name" };
    for (modules.items) |m| if (!text.moduleName(m)) return .{ .ok = false, .err = "unsafe-module-name" };

    const p = try applyLmod(c, module_env, lmod, &.{ "--force", "purge" }, raised);
    var current = p.env orelse return .{ .ok = false, .err = line1(c, if (p.err.len > 0) p.err else "purge failed", 300) };
    const orig_cpuinfo = current.get("EESSI_PROC_CPUINFO");
    const prepared = prepareCpuinfoWithTemp(c, pinnedEnv(c, current, pin), c.env);
    const e = applyLmod(c, prepared.child, lmod, &.{ "load", eessi }, raised) catch |err| {
        if (prepared.cleanup) |cp| unlinkPath(cp);
        return err;
    };
    if (prepared.cleanup) |cp| unlinkPath(cp);
    var next_env = e.env orelse {
        const miss = a.dupe([]const u8, &.{eessi}) catch &.{};
        return .{ .ok = false, .missing = miss, .err = line1(c, if (e.err.len > 0) e.err else "EESSI load failed", 300) };
    };
    if (orig_cpuinfo) |orig| {
        next_env.put(a, "EESSI_PROC_CPUINFO", orig) catch main_mod.oom();
    } else {
        _ = next_env.orderedRemove("EESSI_PROC_CPUINFO");
    }
    current = next_env;
    if (modules.items.len > 0) {
        var args: std.ArrayList([]const u8) = .empty;
        args.append(a, "load") catch main_mod.oom();
        args.appendSlice(a, modules.items) catch main_mod.oom();
        const n = try applyLmod(c, current, lmod, args.items, raised);
        current = n.env orelse blk: {
            const seq = try loadInOrder(c, current, lmod, modules.items, raised);
            if (seq.missing.len > 0)
                return .{ .ok = false, .missing = seq.missing, .err = line1(c, if (seq.err.len > 0) seq.err else "module load failed", 300) };
            break :blk seq.env;
        };
    }
    return .{ .ok = true, .env = current };
}

/// restore_main: one JSON request on stdin, one JSON reply on stdout; 1 with
/// a message on stderr when the request cannot be handled at all.
pub fn restoreMain(c: *Ctx) u8 {
    const a = c.arena;
    const input = fsx.readAll(a, 0, 256 << 20) catch {
        writeErr("eessi-arch-guard: restore failed: could not read the request\n");
        return 1;
    };
    // json.loads(bytes.decode("utf-8")): the request must be UTF-8 text.
    if (!std.unicode.utf8ValidateSlice(input)) {
        writeErr("eessi-arch-guard: restore failed: the request is not UTF-8\n");
        return 1;
    }
    const payload = json.parse(a, input) catch {
        writeErr("eessi-arch-guard: restore failed: the request is not JSON\n");
        return 1;
    };
    if (payload != .object) {
        writeErr("eessi-arch-guard: restore failed: the request is not a JSON object\n");
        return 1;
    }
    var raised: []const u8 = "";
    const reply = doRestore(c, payload, &raised) catch {
        const msg = a.print("eessi-arch-guard: restore failed: {s}\n", .{line1(c, raised, 300)}) catch "eessi-arch-guard: restore failed\n";
        writeErr(msg);
        return 1;
    };
    var out: std.ArrayList(u8) = .empty;
    writeReply(c, &out, reply) catch return 1;
    common.cli.put(1, out.items);
    return 0;
}

fn writeReply(c: *Ctx, out: *std.ArrayList(u8), r: Reply) Allocator.Error!void {
    const a = c.arena;
    try out.appendSlice(a, if (r.ok) "{\"ok\": true" else "{\"ok\": false");
    if (r.env) |e| {
        try out.appendSlice(a, ", \"env\": ");
        try wireMap(c, out, e);
    }
    try out.appendSlice(a, ", \"missing\": [");
    for (r.missing, 0..) |m, i| {
        if (i > 0) try out.appendSlice(a, ", ");
        try json.writeCacheString(out, a, m);
    }
    try out.appendSlice(a, "], \"error\": ");
    try json.writeCacheString(out, a, r.err);
    try out.append(a, '}');
}

fn writeErr(s: []const u8) void {
    common.cli.put(2, s);
}

// ---- the decision ------------------------------------------------------

fn side(s: []const u8) []const u8 {
    if (s.len == 0 or text.hasLineBreakOrNul(s)) return "unrecognized";
    return s;
}

/// A failed decision, as ``--json`` and syslog report it.
pub fn failureOutcome(a: Allocator, was: []const u8, now: []const u8, message: []const u8, detail: ?[]const u8) Outcome {
    var ch: Changes = .empty;
    const token = a.print("{s}->{s}", .{ side(was), side(now) }) catch "unrecognized->unrecognized";
    ch.put(a, "EESSI_ARCH_GUARD_FAILED", token) catch main_mod.oom();
    const msg = text.oneLine(a, message, 500) catch message;
    const det = if (detail) |d| (if (d.len > 0) (text.oneLine(a, d, 500) catch d) else null) else null;
    return .{ .changes = ch, .message = msg, .kind = .failed, .detail = det };
}

fn failed(c: *Ctx, was: []const u8, now: []const u8, message: []const u8, detail: ?[]const u8) Outcome {
    return failureOutcome(c.arena, was, now, message, detail);
}

fn fmt(c: *Ctx, comptime f: []const u8, args: anytype) []const u8 {
    return c.arena.print(f, args) catch "eessi-arch-guard: out of memory";
}

const Loaded = struct { eessi: []const u8 = "", others: []const []const u8 = &.{}, err: []const u8 = "" };

fn parseLoaded(c: *Ctx, env: Map) Loaded {
    const raw = env.get("LOADEDMODULES") orelse empty;
    var names: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, raw, ':');
    while (it.next()) |p| if (p.len > 0) names.append(c.arena, p) catch main_mod.oom();
    if (names.items.len == 0) return .{ .err = "LOADEDMODULES is empty" };
    for (names.items) |n| if (!text.moduleName(n)) return .{ .err = "unsafe-module-name" };
    var eessi: ?[]const u8 = null;
    var count: usize = 0;
    for (names.items) |n| if (std.mem.startsWith(u8, n, "EESSI/")) {
        eessi = n;
        count += 1;
    };
    if (count != 1 or !text.eessiModule(eessi.?)) return .{ .err = "eessi-module-name" };
    var others: std.ArrayList([]const u8) = .empty;
    for (names.items) |n| if (!std.mem.eql(u8, n, eessi.?)) others.append(c.arena, n) catch main_mod.oom();
    return .{ .eessi = eessi.?, .others = others.items };
}

fn restoreMismatch(new: Map, now: []const u8, now_accel: []const u8, compare_accel: bool, eessi: []const u8, others: []const []const u8, c: *Ctx) []const u8 {
    if (!std.mem.eql(u8, new.get("EESSI_SOFTWARE_SUBDIR") orelse empty, now)) return "reload did not select this node's CPU target";
    if (compare_accel and !std.mem.eql(u8, new.get("EESSI_ACCEL_SUBDIR") orelse empty, now_accel)) return "reload did not select this node's accelerator target";
    var loaded: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, new.get("LOADEDMODULES") orelse empty, ':');
    while (it.next()) |p| if (p.len > 0) loaded.append(c.arena, p) catch main_mod.oom();
    if (!text.inList(loaded.items, eessi)) return "reload did not load the EESSI module";
    for (others) |n| if (!text.inList(loaded.items, n)) return fmt(c, "reload did not load {s}", .{n});
    return "";
}

fn nonEmpty(env: Map, k: []const u8) bool {
    return (env.get(k) orelse empty).len > 0;
}

fn softwareRoot(c: *Ctx, env: Map) ?[]const u8 {
    const prefix = env.get("EESSI_PREFIX") orelse empty;
    const os_type = env.get("EESSI_OS_TYPE") orelse empty;
    if (!text.safeAbsolute(prefix) or !text.safeRelSubdir(os_type)) return null;
    const root = std.fs.path.join(c.arena, &.{ prefix, "software", os_type }) catch return null;
    if (!isDir(root)) return null;
    return root;
}

fn invalidGuardEnvironment(c: *Ctx, env: Map) ?[]const u8 {
    const override = env.get("EESSI_SOFTWARE_SUBDIR_OVERRIDE") orelse empty;
    if (override.len > 0) {
        if (!std.mem.eql(u8, text.pyStrip(override), override) or std.mem.endsWith(u8, override, "/"))
            return fmt(c, "EESSI_SOFTWARE_SUBDIR_OVERRIDE is not a safe subdirectory: {s}", .{override});
        if (text.safeRelSubdir(override)) {
            if (softwareRoot(c, env)) |root| {
                const target = std.fs.path.join(c.arena, &.{ root, override, "software" }) catch return "out of memory";
                if (!isDir(target))
                    return fmt(c, "EESSI_SOFTWARE_SUBDIR_OVERRIDE is not an installed target: {s}", .{override});
            }
        }
    }
    const accel = env.get("EESSI_ACCEL_SUBDIR") orelse empty;
    if (accel.len > 0) {
        if (!std.mem.eql(u8, text.pyStrip(accel), accel) or !text.accelSubdir(accel))
            return fmt(c, "EESSI_ACCEL_SUBDIR is not a valid accelerator subdirectory: {s}", .{accel});
    }
    return null;
}

/// decide: compare the loaded targets with this node's and, when they
/// differ, reload the modules through the restore helper and return the
/// delta. `shell` selects the --shell output, whose values may span lines.
/// Returns error.Deadline or error.Terminated when the limit stops it.
pub fn decide(c: *Ctx, env: Map, shell: bool) Stop!Outcome {
    if (c.limit) |l| try l.check();
    if (std.mem.eql(u8, env.get("EESSI_ARCH_GUARD") orelse empty, "0"))
        return .{ .kind = .skip, .detail = "EESSI_ARCH_GUARD=0" };
    const was = env.get("EESSI_SOFTWARE_SUBDIR") orelse empty;
    if (was.len == 0) return .{ .kind = .skip, .detail = "EESSI is not loaded" };
    const software_override = env.get("EESSI_SOFTWARE_SUBDIR_OVERRIDE") orelse empty;
    if (invalidGuardEnvironment(c, env)) |invalid|
        return failed(c, was, "invalid-environment", fmt(c, "eessi-arch-guard: {s}; environment left as it was", .{invalid}), null);
    if (software_override.len > 0 and text.safeRelSubdir(software_override))
        return .{ .kind = .skip, .detail = "EESSI_SOFTWARE_SUBDIR_OVERRIDE is set" };
    if (nonEmpty(env, "EESSI_ARCHDETECT_OPTIONS_OVERRIDE")) return .{ .kind = .skip, .detail = "EESSI_ARCHDETECT_OPTIONS_OVERRIDE is set" };

    const cpu = try probeCpu(c, env);
    if (cpu.status == .@"error")
        return failed(c, was, "archdetect-failed", fmt(c, "eessi-arch-guard: archdetect failed ({s}); environment left as it was", .{cpu.err}), null);
    if (cpu.status == .none) {
        if (std.mem.eql(u8, env.get("EESSI_ARCH_GUARD_MISSING") orelse empty, "ignore"))
            return .{ .kind = .skip, .detail = "no EESSI software directory (ignored)" };
        const shown = if (cpu.candidates.len > 0) std.mem.join(c.arena, " ", cpu.candidates) catch "(none)" else "(none)";
        return failed(c, was, "no-software-dir", fmt(c, "eessi-arch-guard: no EESSI software directory for this node under {s} (candidates {s}); modules were loaded for {s}; environment left as it was", .{ env.get("EESSI_PREFIX") orelse empty, shown, was }), null);
    }

    const compare_accel = !nonEmpty(env, "EESSI_ACCELERATOR_TARGET_OVERRIDE");
    const was_accel = env.get("EESSI_ACCEL_SUBDIR") orelse empty;
    var now_accel = was_accel;
    if (compare_accel) {
        const accel = try probeAccel(c, env);
        if (accel.status == .@"error")
            return failed(c, was, "archdetect-failed", fmt(c, "eessi-arch-guard: archdetect failed ({s}); environment left as it was", .{accel.err}), null);
        now_accel = accel.subdir;
    }
    const now = cpu.target;
    if (std.mem.eql(u8, now, was) and std.mem.eql(u8, now_accel, was_accel))
        return .{ .kind = .noop, .detail = "target already matches this node" };

    var was_label = was;
    var now_label = now;
    if (compare_accel and !std.mem.eql(u8, was_accel, now_accel)) {
        was_label = fmt(c, "{s} {s}", .{ was, if (was_accel.len > 0) was_accel else "no accel" });
        now_label = fmt(c, "{s} {s}", .{ now, if (now_accel.len > 0) now_accel else "no accel" });
    }

    const loaded = parseLoaded(c, env);
    if (loaded.err.len > 0) {
        const unsafe_name = std.mem.eql(u8, loaded.err, "unsafe-module-name");
        const why = if (unsafe_name) "a loaded module name is not safe to pass to Lmod" else "the loaded module list has no single EESSI/<version> entry";
        return failed(c, was_label, if (unsafe_name) "unsafe-module-name" else "restore-failed", fmt(c, "eessi-arch-guard: {s}; environment left as it was", .{why}), null);
    }
    if (!lmodInvocable(env.get("LMOD_CMD") orelse empty))
        return failed(c, was_label, "lmod-unavailable", fmt(c, "eessi-arch-guard: LMOD_CMD is missing or not executable; this node is {s}, the modules were loaded for {s}; environment left as it was", .{ now_label, was_label }), null);

    var restore_env = env;
    if (software_override.len > 0 and !text.safeRelSubdir(software_override)) {
        restore_env = envmap.clone(c.arena, env) catch main_mod.oom();
        _ = restore_env.orderedRemove("EESSI_SOFTWARE_SUBDIR_OVERRIDE");
    }
    const result = try callRestore(c, restore_env, loaded.eessi, loaded.others, cpu.override);
    if (!result.ran)
        return failed(c, was_label, "restore-failed", fmt(c, "eessi-arch-guard: restore failed ({s}); environment left as it was", .{line1(c, result.err, 200)}), null);
    if (!result.ok or result.env == null) {
        var why: []const u8 = "module load failed";
        if (result.missing.len > 0) {
            var names = std.mem.join(c.arena, " ", result.missing) catch "";
            if (result.missing.len >= diagnose_limit and loaded.others.len > result.missing.len) names = fmt(c, "{s} (stopped after {d})", .{ names, diagnose_limit });
            why = fmt(c, "not available for this target: {s}", .{names});
        }
        return failed(c, was_label, now_label, fmt(c, "eessi-arch-guard: this node is {s}, the modules were loaded for {s}; {s}; environment left as it was", .{ now_label, was_label, why }), result.err);
    }
    const mismatch = restoreMismatch(result.env.?, now, now_accel, compare_accel, loaded.eessi, loaded.others, c);
    if (mismatch.len > 0)
        return failed(c, was_label, "restore-failed", fmt(c, "eessi-arch-guard: {s}; environment left as it was", .{mismatch}), null);

    var d = (envmap.delta(c.arena, env, result.env.?, shell) catch null) orelse
        return failed(c, was_label, "unsafe-environment", "eessi-arch-guard: reload produced a value this output mode cannot carry; environment left as it was", null);
    d.put(c.arena, "EESSI_ARCH_GUARD_RELOADED", fmt(c, "{s}->{s}", .{ side(was_label), side(now_label) })) catch main_mod.oom();
    if (env.get("EESSI_ARCH_GUARD_FAILED") != null and !d.contains("EESSI_ARCH_GUARD_FAILED"))
        d.put(c.arena, "EESSI_ARCH_GUARD_FAILED", null) catch main_mod.oom();
    const message = fmt(c, "eessi-arch-guard: reloaded modules for {s} (loaded for {s})", .{ now_label, was_label });
    if (!shell) {
        _ = envmap.render(c.arena, d, message, false, true) catch
            return failed(c, was_label, "unsafe-environment", "eessi-arch-guard: reload produced a prolog line past the Slurm 17.11 read; environment left as it was", null);
    }
    return .{ .changes = d, .message = message, .kind = .reloaded };
}

// ---- the cache ---------------------------------------------------------

const gpu_scope_keys = [_][]const u8{
    "CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL",
    "SLURM_STEP_GPUS",      "SLURM_GPUS_ON_NODE",
};

fn taskScopeKey(arena: Allocator, env: Map) []const u8 {
    const allowed = taskCpuIds(arena, env) orelse return "";
    var any = false;
    var cpu: usize = 0;
    while (cpu < cpu_id_max) : (cpu += 1) if (allowed.has(cpu)) {
        any = true;
        break;
    };
    if (!any) return "-";
    var out: std.ArrayList(u8) = .empty;
    var first = true;
    cpu = 0;
    while (cpu < cpu_id_max) : (cpu += 1) {
        if (!allowed.has(cpu)) continue;
        if (!first) out.append(arena, ',') catch return "";
        first = false;
        out.print(arena, "{d}", .{cpu}) catch return "";
    }
    return out.toOwnedSlice(arena) catch "";
}

fn gpuScopeKey(arena: Allocator, env: Map) []const u8 {
    var out: std.ArrayList(u8) = .empty;
    var first = true;
    for (gpu_scope_keys) |k| {
        const v = env.get(k) orelse continue;
        if (!first) out.append(arena, 0) catch return "";
        first = false;
        out.appendSlice(arena, k) catch return "";
        out.append(arena, '=') catch return "";
        out.appendSlice(arena, v) catch return "";
    }
    return out.toOwnedSlice(arena) catch "";
}

fn cacheDigest(c: *Ctx, env: Map) []const u8 {
    var h = std.crypto.hash.sha2.Sha256.init(.{});
    for (key_fields, 0..) |k, i| {
        if (i > 0) h.update("\x00");
        h.update(env.get(k) orelse empty);
    }
    h.update("\x00");
    h.update(taskScopeKey(c.arena, env));
    h.update("\x00");
    h.update(gpuScopeKey(c.arena, env));
    var out: [32]u8 = undefined;
    h.final(&out);
    const hex = std.fmt.bytesToHex(out, .lower);
    return c.arena.dupe(u8, hex[0..16]) catch "0000000000000000";
}

fn safeComponent(c: *Ctx, s: []const u8) []const u8 {
    if (text.safeComponent(s)) return s;
    var out: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(s, &out, .{});
    const hex = std.fmt.bytesToHex(out, .lower);
    return c.arena.dupe(u8, hex[0..16]) catch "0000000000000000";
}

fn cacheDirectory(c: *Ctx, env: Map) ?[]const u8 {
    if (c.strict) {
        var why: []const u8 = "";
        return restoreWorkBase(c, env, &why) catch null;
    }
    const tmp = env.get("TMPDIR") orelse empty;
    if (tmp.len > 0 and text.safeAbsolute(tmp) and isDir(tmp)) return tmp;
    if (isDir("/tmp")) return "/tmp";
    return null;
}

const CachePath = struct { dir: []const u8, name: []const u8 };

/// cache_file_path: .eessi-arch-guard.<uid>.<node>.<job>.<step>.<mode>.<digest>
fn cachePath(c: *Ctx, env: Map, shell: bool) ?CachePath {
    const job = env.get("SLURM_JOB_ID") orelse empty;
    if (job.len == 0) return null;
    const dir = cacheDirectory(c, env) orelse return null;
    const step = env.get("SLURM_STEP_ID") orelse "batch";
    var uts: linux.utsname = undefined;
    _ = linux.uname(&uts);
    const node = std.mem.sliceTo(&uts.nodename, 0);
    const name = c.arena.print(".eessi-arch-guard.{d}.{s}.{s}.{s}.{s}.{s}", .{
        sys.getuid(),
        safeComponent(c, node),
        safeComponent(c, job),
        safeComponent(c, step),
        if (shell) "shell" else "prolog",
        cacheDigest(c, env),
    }) catch return null;
    return .{ .dir = dir, .name = name };
}

fn kindName(k: Kind) []const u8 {
    return @tagName(k);
}

fn readCache(c: *Ctx, dirfd: sys.fd_t, name: []const u8, shell: bool) ?Outcome {
    const a = c.arena;
    const blob = (state.readPrivate(a, dirfd, name, cache_read_max) catch return null) orelse return null;
    if (!std.unicode.utf8ValidateSlice(blob)) return null;
    const data = json.parse(a, blob) catch return null;
    if (data != .object) return null;
    // data.get("v") != 1 in Python: 1, 1.0 and true all equal 1.
    const v = data.get("v") orelse return null;
    switch (v) {
        .number => |n| if ((std.fmt.parseFloat(f64, n) catch return null) != 1.0) return null,
        .bool => |b| if (!b) return null,
        else => return null,
    }
    var message: ?[]const u8 = null;
    if (data.get("message")) |m| switch (m) {
        .null => {},
        .string => |s| {
            const t = json.decodeCache(a, s) catch return null;
            if (std.mem.findAny(u8, t, "\n\r\x00") != null) return null;
            message = t;
        },
        else => return null,
    };
    var detail: ?[]const u8 = null;
    if (data.get("detail")) |d| switch (d) {
        .null => {},
        .string => |s| detail = json.decodeCache(a, s) catch return null,
        else => return null,
    };
    const ch = data.get("changes") orelse return null;
    if (ch != .object) return null;
    // json.loads keeps the last value of a repeated key.
    var last: std.StringArrayHashMapUnmanaged(json.Value) = .empty;
    for (ch.object) |mem| {
        const key = json.decodeCache(a, mem.key) catch return null;
        last.put(a, key, mem.value) catch return null;
    }
    var changes: Changes = .empty;
    var lit = last.iterator();
    while (lit.next()) |mem_kv| {
        const mem = .{ .key = mem_kv.key_ptr.*, .value = mem_kv.value_ptr.* };
        const key = mem.key;
        if (!text.envName(key) or text.neverCache(key)) return null;
        switch (mem.value) {
            .null => changes.put(a, key, null) catch return null,
            .string => |s| {
                const val = json.decodeCache(a, s) catch return null;
                if (!text.valueEmitOk(val, shell)) return null;
                changes.put(a, key, val) catch return null;
            },
            else => return null,
        }
    }
    const k = data.get("kind") orelse return null;
    if (k != .string) return null;
    const kind = std.meta.stringToEnum(Kind, json.decodeCache(a, k.string) catch return null) orelse return null;
    return .{ .changes = changes, .message = message, .kind = kind, .detail = detail };
}

fn writeCache(c: *Ctx, dirfd: sys.fd_t, name: []const u8, o: Outcome) void {
    const a = c.arena;
    var out: std.ArrayList(u8) = .empty;
    (struct {
        fn w(arena: Allocator, buf: *std.ArrayList(u8), oc: Outcome) Allocator.Error!void {
            try buf.appendSlice(arena, "{\"v\": 1, \"kind\": ");
            try json.writeCacheString(buf, arena, kindName(oc.kind));
            try buf.appendSlice(arena, ", \"message\": ");
            if (oc.message) |m| try json.writeCacheString(buf, arena, m) else try buf.appendSlice(arena, "null");
            try buf.appendSlice(arena, ", \"detail\": ");
            if (oc.detail) |d| try json.writeCacheString(buf, arena, d) else try buf.appendSlice(arena, "null");
            try buf.appendSlice(arena, ", \"changes\": {");
            var it = oc.changes.iterator();
            var first = true;
            while (it.next()) |kv| {
                if (!first) try buf.appendSlice(arena, ", ");
                first = false;
                try json.writeCacheString(buf, arena, kv.key_ptr.*);
                try buf.appendSlice(arena, ": ");
                if (kv.value_ptr.*) |v| try json.writeCacheString(buf, arena, v) else try buf.appendSlice(arena, "null");
            }
            try buf.appendSlice(arena, "}}\n");
        }
    }).w(a, &out, o) catch return;
    state.writePrivate(a, dirfd, name, out.items) catch {};
}

/// _lock_exclusive: wait for the cache lock. Under a deadline the wait
/// polls and only "would block" waits again, as Python retries only
/// BlockingIOError; any other flock error is an OSError (error.Os).
fn lockExclusive(c: *Ctx, fd: sys.fd_t) Stop!void {
    const l = c.limit orelse {
        state.lockExclusive(fd, null) catch |err| {
            os_failure = .{ .text = if (common.sys.asError(err)) |e| common.sys.describe(e) else @errorName(err) };
            return error.Os;
        };
        return;
    };
    while (true) {
        try l.check();
        const rc = linux.flock(fd, std.posix.LOCK.EX | std.posix.LOCK.NB);
        switch (linux.errno(rc)) {
            .SUCCESS => return,
            .AGAIN, .INTR => {},
            else => |e| {
                os_failure = .{ .class = osErrorClass(e), .text = common.sys.describeErrno(e) };
                return error.Os;
            },
        }
        const pause = @max(@min(lock_poll_ms, l.remainingMs()), 0);
        var ts: linux.timespec = .{ .sec = 0, .nsec = @intCast(pause * std.time.ns_per_ms) };
        _ = linux.nanosleep(&ts, null);
    }
}

test "a flock error other than would-block is an OSError, not a wait" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    var limit: Limit = .{ .deadline_ms = nowMs() + 60_000 };
    var c: Ctx = .{ .arena = arena_state.allocator(), .env = .empty, .strict = false, .self_exe = "" };
    c.limit = &limit;
    // flock refuses a descriptor opened O_PATH with EBADF.
    const fd = try sys.openat(fsx.cwd, "/", .{ .PATH = true }, 0);
    defer sys.close(fd);
    const start = nowMs();
    try std.testing.expectError(error.Os, lockExclusive(&c, fd));
    try std.testing.expect(nowMs() - start < 1000);
    try std.testing.expectEqualStrings("OSError", os_failure.class);
    try std.testing.expectEqualStrings("[Errno 9] Bad file descriptor", os_failure.text);
}

/// _read_cache's open of the cache directory, which no handler around it
/// catches: an OSError goes up to main, as error.Os.
fn openCacheDir(c: *Ctx, dir: []const u8) error{Os}!sys.fd_t {
    return fsx.openDir(fsx.cwd, dir, true) catch |err| {
        // sys.z refuses a path too long for the kernel before any call.
        const e: linux.E = if (err == error.NameTooLong) .NAMETOOLONG else common.sys.last_errno;
        os_failure = .{
            .class = osErrorClass(e),
            .text = common.pytext.osErrorErrno(c.arena, e, dir) catch main_mod.oom(),
        };
        return error.Os;
    };
}

test "an unopenable cache directory is the OSError Python raises" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    var c: Ctx = .{ .arena = arena_state.allocator(), .env = .empty, .strict = false, .self_exe = "" };
    try std.testing.expectError(error.Os, openCacheDir(&c, "/nonexistent/it's"));
    try std.testing.expectEqualStrings("FileNotFoundError", os_failure.class);
    try std.testing.expectEqualStrings("[Errno 2] No such file or directory: \"/nonexistent/it's\"", os_failure.text);
    try std.testing.expectError(error.Os, openCacheDir(&c, "/proc/self/exe/x"));
    try std.testing.expectEqualStrings("NotADirectoryError", os_failure.class);
    try std.testing.expectEqualStrings("[Errno 20] Not a directory: '/proc/self/exe/x'", os_failure.text);
    try std.testing.expectError(error.Os, openCacheDir(&c, "/nonexistent/a'b\"c"));
    try std.testing.expectEqualStrings("FileNotFoundError", os_failure.class);
    try std.testing.expectEqualStrings("[Errno 2] No such file or directory: '/nonexistent/a\\'b\"c'", os_failure.text);
    try std.testing.expectError(error.Os, openCacheDir(&c, "/nonexistent/a\\b\n\xff"));
    try std.testing.expectEqualStrings("FileNotFoundError", os_failure.class);
    try std.testing.expectEqualStrings("[Errno 2] No such file or directory: '/nonexistent/a\\\\b\\n\\udcff'", os_failure.text);
    var long: [sys.path_max + 8]u8 = @splat('a');
    long[0] = '/';
    try std.testing.expectError(error.Os, openCacheDir(&c, &long));
    try std.testing.expectEqualStrings("OSError", os_failure.class);
    try std.testing.expect(std.mem.startsWith(u8, os_failure.text, "[Errno 36] File name too long: '/aaaa"));
}

/// cached_decide: `decide`, once per job step, node, user, output mode and
/// cache key. The first task takes the lock and writes its outcome; the
/// others wait for the lock and read it. Without SLURM_JOB_ID, or without a
/// usable cache directory or lock file, every task decides on its own.
pub fn cachedDecide(c: *Ctx, env: Map, shell: bool) Stop!Outcome {
    last_cached = false;
    const p = cachePath(c, env, shell) orelse return decide(c, env, shell);
    const dirfd = fsx.openDir(fsx.cwd, p.dir, true) catch return decide(c, env, shell);
    defer sys.close(dirfd);
    const lock_name = c.arena.print("{s}.lock", .{p.name}) catch return decide(c, env, shell);
    // A lock file someone else made is neither locked on nor changed:
    // decide without the cache.
    const lockfd = (state.openPrivate(dirfd, lock_name, true) catch return decide(c, env, shell)) orelse return decide(c, env, shell);
    defer sys.close(lockfd);
    try lockExclusive(c, lockfd);
    if (readCache(c, dirfd, p.name, shell)) |o| {
        last_cached = true;
        return o;
    }
    if (c.limit) |l| try l.check();
    const outcome = try decide(c, env, shell);
    if (c.limit) |l| l.committing.store(true, .seq_cst);
    defer if (c.limit) |l| l.committing.store(false, .seq_cst);
    writeCache(c, dirfd, p.name, outcome);
    return outcome;
}

/// Read-only decision summary produced by `--dry-run`.
pub const DryRunDecision = struct {
    /// What the guard would decide: `skip`, `noop`, `reloaded`, or `failed`.
    kind: Kind,
    /// CPU target recorded in `EESSI_SOFTWARE_SUBDIR`.
    was_cpu: []const u8 = "",
    /// CPU target selected for this node.
    now_cpu: []const u8 = "",
    /// Accelerator target recorded in `EESSI_ACCEL_SUBDIR`.
    was_accel: []const u8 = "",
    /// Accelerator target selected for this node.
    now_accel: []const u8 = "",
    /// Whether the accelerator target was compared.
    compare_accel: bool = true,
    /// Fallback CPU pin passed to the restore helper when waitpkg dropped the first choice.
    pin: []const u8 = "",
    /// Loaded `EESSI/<version>` module name when checked.
    eessi: []const u8 = "",
    /// Other loaded modules in order when checked.
    modules: []const []const u8 = &.{},
    /// Marker token `was->now` for reload or failure outcomes.
    marker: []const u8 = "",
    /// Short reason text for `skip` and `noop` outcomes.
    detail: ?[]const u8 = null,
    /// Diagnostic or failure message for `reloaded` and `failed` outcomes.
    message: ?[]const u8 = null,
};

/// Result of inspecting the cache file without creating a lock or writing.
pub const CacheProbe = struct {
    /// Cache file path that would be used, or null when no cache applies.
    path: ?[]const u8 = null,
    /// Whether a valid cache entry already exists at `path`.
    hit: bool = false,
    /// Outcome kind stored in that cache entry when `hit` is true.
    cached_kind: ?Kind = null,
};

/// Inspect the cache entry for `env` without creating a lock file or writing.
pub fn probeCacheReadonly(c: *Ctx, env: Map, shell: bool) CacheProbe {
    const p = cachePath(c, env, shell) orelse return .{};
    const full = std.fs.path.join(c.arena, &.{ p.dir, p.name }) catch return .{};
    const dirfd = fsx.openDir(fsx.cwd, p.dir, true) catch return .{ .path = full };
    defer sys.close(dirfd);
    if (readCache(c, dirfd, p.name, shell)) |o| {
        return .{ .path = full, .hit = true, .cached_kind = o.kind };
    }
    return .{ .path = full };
}

fn dryRunFailed(c: *Ctx, was_cpu: []const u8, now_cpu: []const u8, was_accel: []const u8, now_accel: []const u8, compare_accel: bool, pin: []const u8, eessi: []const u8, modules: []const []const u8, was_side: []const u8, tok_side: []const u8, message: []const u8) DryRunDecision {
    return .{
        .kind = .failed,
        .was_cpu = was_cpu,
        .now_cpu = now_cpu,
        .was_accel = was_accel,
        .now_accel = now_accel,
        .compare_accel = compare_accel,
        .pin = pin,
        .eessi = eessi,
        .modules = modules,
        .marker = fmt(c, "{s}->{s}", .{ side(was_side), side(tok_side) }),
        .message = line1(c, message, 500),
    };
}

/// Evaluate the guard decision without running restore or writing any state.
pub fn dryRunDecide(c: *Ctx, env: Map) Stop!DryRunDecision {
    if (c.limit) |l| try l.check();
    if (std.mem.eql(u8, env.get("EESSI_ARCH_GUARD") orelse empty, "0"))
        return .{ .kind = .skip, .detail = "EESSI_ARCH_GUARD=0" };
    const was = env.get("EESSI_SOFTWARE_SUBDIR") orelse empty;
    if (was.len == 0) return .{ .kind = .skip, .detail = "EESSI is not loaded" };
    const software_override = env.get("EESSI_SOFTWARE_SUBDIR_OVERRIDE") orelse empty;
    if (invalidGuardEnvironment(c, env)) |invalid|
        return dryRunFailed(c, was, "", "", "", true, "", "", &.{}, was, "invalid-environment", fmt(c, "eessi-arch-guard: {s}; environment left as it was", .{invalid}));
    if (software_override.len > 0 and text.safeRelSubdir(software_override))
        return .{ .kind = .skip, .was_cpu = was, .detail = "EESSI_SOFTWARE_SUBDIR_OVERRIDE is set" };
    if (nonEmpty(env, "EESSI_ARCHDETECT_OPTIONS_OVERRIDE"))
        return .{ .kind = .skip, .was_cpu = was, .detail = "EESSI_ARCHDETECT_OPTIONS_OVERRIDE is set" };

    const cpu = try probeCpu(c, env);
    if (cpu.status == .@"error")
        return dryRunFailed(c, was, "", "", "", true, "", "", &.{}, was, "archdetect-failed", fmt(c, "eessi-arch-guard: archdetect failed ({s}); environment left as it was", .{cpu.err}));
    if (cpu.status == .none) {
        if (std.mem.eql(u8, env.get("EESSI_ARCH_GUARD_MISSING") orelse empty, "ignore"))
            return .{ .kind = .skip, .was_cpu = was, .detail = "no EESSI software directory (ignored)" };
        const shown = if (cpu.candidates.len > 0) std.mem.join(c.arena, " ", cpu.candidates) catch "(none)" else "(none)";
        return dryRunFailed(c, was, "", "", "", true, "", "", &.{}, was, "no-software-dir", fmt(c, "eessi-arch-guard: no EESSI software directory for this node under {s} (candidates {s}); modules were loaded for {s}; environment left as it was", .{ env.get("EESSI_PREFIX") orelse empty, shown, was }));
    }

    const compare_accel = !nonEmpty(env, "EESSI_ACCELERATOR_TARGET_OVERRIDE");
    const was_accel = env.get("EESSI_ACCEL_SUBDIR") orelse empty;
    var now_accel = was_accel;
    if (compare_accel) {
        const accel = try probeAccel(c, env);
        if (accel.status == .@"error")
            return dryRunFailed(c, was, cpu.target, was_accel, "", compare_accel, cpu.override, "", &.{}, was, "archdetect-failed", fmt(c, "eessi-arch-guard: archdetect failed ({s}); environment left as it was", .{accel.err}));
        now_accel = accel.subdir;
    }
    const now = cpu.target;
    if (std.mem.eql(u8, now, was) and std.mem.eql(u8, now_accel, was_accel)) {
        return .{
            .kind = .noop,
            .was_cpu = was,
            .now_cpu = now,
            .was_accel = was_accel,
            .now_accel = now_accel,
            .compare_accel = compare_accel,
            .pin = cpu.override,
            .detail = "target already matches this node",
        };
    }

    var was_label = was;
    var now_label = now;
    if (compare_accel and !std.mem.eql(u8, was_accel, now_accel)) {
        was_label = fmt(c, "{s} {s}", .{ was, if (was_accel.len > 0) was_accel else "no accel" });
        now_label = fmt(c, "{s} {s}", .{ now, if (now_accel.len > 0) now_accel else "no accel" });
    }

    const loaded = parseLoaded(c, env);
    if (loaded.err.len > 0) {
        const unsafe_name = std.mem.eql(u8, loaded.err, "unsafe-module-name");
        const why = if (unsafe_name) "a loaded module name is not safe to pass to Lmod" else "the loaded module list has no single EESSI/<version> entry";
        return dryRunFailed(c, was, now, was_accel, now_accel, compare_accel, cpu.override, "", &.{}, was_label, if (unsafe_name) "unsafe-module-name" else "restore-failed", fmt(c, "eessi-arch-guard: {s}; environment left as it was", .{why}));
    }
    if (!lmodInvocable(env.get("LMOD_CMD") orelse empty))
        return dryRunFailed(c, was, now, was_accel, now_accel, compare_accel, cpu.override, loaded.eessi, loaded.others, was_label, "lmod-unavailable", fmt(c, "eessi-arch-guard: LMOD_CMD is missing or not executable; this node is {s}, the modules were loaded for {s}; environment left as it was", .{ now_label, was_label }));

    var work_why: []const u8 = "";
    _ = restoreWorkBase(c, env, &work_why) catch
        return dryRunFailed(c, was, now, was_accel, now_accel, compare_accel, cpu.override, loaded.eessi, loaded.others, was_label, "restore-failed", fmt(c, "eessi-arch-guard: restore failed (restore workspace unavailable: {s}); environment left as it was", .{work_why}));

    return .{
        .kind = .reloaded,
        .was_cpu = was,
        .now_cpu = now,
        .was_accel = was_accel,
        .now_accel = now_accel,
        .compare_accel = compare_accel,
        .pin = cpu.override,
        .eessi = loaded.eessi,
        .modules = loaded.others,
        .marker = fmt(c, "{s}->{s}", .{ side(was_label), side(now_label) }),
        .message = fmt(c, "eessi-arch-guard: would reload modules for {s} (loaded for {s})", .{ now_label, was_label }),
    };
}

/// Evaluate a dry-run decision under one deadline without touching the cache.
pub fn limitedDryRun(c: *Ctx, env: Map, timeout_s: f64) error{Os}!DryRunDecision {
    var limit: Limit = .{ .deadline_ms = nowMs() + @as(i64, @intFromFloat(@min(timeout_s * 1000.0, 9.2e15))) };
    c.limit = &limit;
    installLimit(&limit);
    defer {
        removeLimit(&limit);
        c.limit = null;
    }
    const was = env.get("EESSI_SOFTWARE_SUBDIR") orelse empty;
    return dryRunDecide(c, env) catch |err| switch (err) {
        error.Deadline => dryRunFailed(c, was, "", "", "", true, "", "", &.{}, was, "timeout", "eessi-arch-guard: decision exceeded the time limit; environment left as it was"),
        error.Terminated => dryRunFailed(c, was, "", "", "", true, "", "", &.{}, was, "terminated", "eessi-arch-guard: decision was terminated; environment left as it was"),
        error.Os => return error.Os,
    };
}

/// The decision under one deadline. A timeout or SIGTERM is not cached.
pub fn limitedDecide(c: *Ctx, env: Map, shell: bool, timeout_s: f64) error{Os}!Outcome {
    last_cached = false;
    var limit: Limit = .{ .deadline_ms = nowMs() + @as(i64, @intFromFloat(@min(timeout_s * 1000.0, 9.2e15))) };
    c.limit = &limit;
    installLimit(&limit);
    defer {
        removeLimit(&limit);
        c.limit = null;
    }
    const was = env.get("EESSI_SOFTWARE_SUBDIR") orelse empty;
    return cachedDecide(c, env, shell) catch |err| switch (err) {
        error.Deadline => {
            if (cachePath(c, env, shell)) |p| {
                const dirfd = try openCacheDir(c, p.dir);
                defer sys.close(dirfd);
                if (readCache(c, dirfd, p.name, shell)) |o| {
                    last_cached = true;
                    return o;
                }
            }
            return failed(c, was, "timeout", "eessi-arch-guard: decision exceeded the time limit; environment left as it was", null);
        },
        error.Terminated => return failed(c, was, "terminated", "eessi-arch-guard: decision was terminated; environment left as it was", null),
        error.Os => return error.Os,
    };
}

test "dryRunDecide handles skip and missing EESSI without writing" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var env: Map = .empty;
    var c: Ctx = .{ .arena = a, .env = env, .strict = false, .self_exe = "" };
    const d1 = try dryRunDecide(&c, env);
    try std.testing.expectEqual(Kind.skip, d1.kind);
    try std.testing.expectEqualStrings("EESSI is not loaded", d1.detail.?);
    try env.put(a, "EESSI_ARCH_GUARD", "0");
    try env.put(a, "EESSI_SOFTWARE_SUBDIR", "x86_64/amd/zen4");
    const d2 = try dryRunDecide(&c, env);
    try std.testing.expectEqual(Kind.skip, d2.kind);
    try std.testing.expectEqualStrings("EESSI_ARCH_GUARD=0", d2.detail.?);
}

test "mkdtemp delegates to fsx.mkdtemp with the guard prefix" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var cwd_buf: [sys.path_max]u8 = undefined;
    const here = try fsx.realpath(fsx.cwd, ".", &cwd_buf);
    const base = try std.fmt.allocPrint(a, "{s}/.zig-cache/eag-mkdtemp-{d}", .{ here, linux.getpid() });
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, base)).ptr, 0o700);
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};

    var c: Ctx = .{ .arena = a, .env = .empty, .strict = false, .self_exe = "" };
    const created = mkdtemp(&c, base).?;
    const expected_prefix = try std.fmt.allocPrint(a, "{s}/eessi-arch-guard-", .{base});
    try std.testing.expect(std.mem.startsWith(u8, created, expected_prefix));
    try std.testing.expectEqual(expected_prefix.len + 8, created.len);
    const st = try fsx.statAt(fsx.cwd, created, false);
    try std.testing.expect(st.isDir());
    try std.testing.expectEqual(@as(u32, 0), st.perm() & 0o077);

    const missing = try std.fmt.allocPrint(a, "{s}/missing", .{base});
    try std.testing.expect(mkdtemp(&c, missing) == null);
}

test "prepareCpuinfo writes scoped cpuinfo under TMPDIR" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var cwd_buf: [sys.path_max]u8 = undefined;
    const here = try fsx.realpath(fsx.cwd, ".", &cwd_buf);
    const base = try std.fmt.allocPrint(a, "{s}/.zig-cache/eag-cpuinfo-{d}", .{ here, linux.getpid() });
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, base)).ptr, 0o700);
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};

    const tmp_dir = try std.fmt.allocPrint(a, "{s}/tmp", .{base});
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, tmp_dir)).ptr, 0o700);
    const cpuinfo_path = try std.fmt.allocPrint(a, "{s}/cpuinfo", .{base});
    const cpuinfo =
        \\processor : 0
        \\vendor_id : GenuineIntel
        \\flags : avx2
        \\
        \\processor : 1
        \\vendor_id : GenuineIntel
        \\flags : avx2 waitpkg
        \\
    ;
    try std.testing.expect(writePrivateNew(cpuinfo_path, cpuinfo, 0o600));

    var env: Map = .empty;
    try env.put(a, "TMPDIR", tmp_dir);
    try env.put(a, "EESSI_PROC_CPUINFO", cpuinfo_path);
    try env.put(a, "SLURM_CPU_BIND_LIST", "1");

    var c: Ctx = .{ .arena = a, .env = env, .strict = false, .self_exe = "" };
    const prepared = prepareCpuinfo(&c, env);
    defer if (prepared.cleanup) |p| unlinkPath(p);
    const expected_prefix = try std.fmt.allocPrint(a, "{s}/eessi-arch-guard-cpu-", .{tmp_dir});
    try std.testing.expect(std.mem.startsWith(u8, prepared.path, expected_prefix));
}

test "scopeCpuinfoWithBase maps LXCFS renumbered blocks and keeps gapped logical IDs" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    const lxcfs_cpuinfo =
        \\processor : 0
        \\vendor_id : GenuineIntel
        \\core id : 1
        \\flags : avx2 fma
        \\
        \\processor : 1
        \\vendor_id : GenuineIntel
        \\core id : 3
        \\flags : avx2 fma avx512f waitpkg
        \\
    ;
    var base_1_3: CpuSet = .{};
    base_1_3.add(1);
    base_1_3.add(3);
    const both = scopeCpuinfoWithBase(a, lxcfs_cpuinfo, base_1_3, base_1_3).?;
    try std.testing.expect(std.mem.find(u8, both, "waitpkg") == null);
    try std.testing.expect(std.mem.find(u8, both, "avx2") != null);

    var only_3: CpuSet = .{};
    only_3.add(3);
    const three = scopeCpuinfoWithBase(a, lxcfs_cpuinfo, only_3, base_1_3).?;
    try std.testing.expect(std.mem.find(u8, three, "waitpkg") != null);

    const gapped_cpuinfo =
        \\processor : 0
        \\flags : avx2
        \\
        \\processor : 2
        \\flags : avx2 avx512f
        \\
        \\processor : 4
        \\flags : avx2 avx512f waitpkg
        \\
        \\processor : 6
        \\flags : sse2
        \\
    ;
    var allow_2_4: CpuSet = .{};
    allow_2_4.add(2);
    allow_2_4.add(4);
    const gapped = scopeCpuinfoWithBase(a, gapped_cpuinfo, allow_2_4, allow_2_4).?;
    try std.testing.expect(std.mem.find(u8, gapped, "processor : 2\n") != null);
    try std.testing.expect(std.mem.find(u8, gapped, "avx512f") != null);
    try std.testing.expect(std.mem.find(u8, gapped, "waitpkg") == null);
}

test "cgroupCpusFrom narrows wider sched affinity on cgroup v2 layouts" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    var cwd_buf: [sys.path_max]u8 = undefined;
    const here = try fsx.realpath(fsx.cwd, ".", &cwd_buf);
    const base = try std.fmt.allocPrint(a, "{s}/.zig-cache/eag-cgroupv2-{d}", .{ here, linux.getpid() });
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, base)).ptr, 0o700);
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};

    const cg_root = try std.fmt.allocPrint(a, "{s}/cgroup", .{base});
    const slice_dir = try std.fmt.allocPrint(a, "{s}/system.slice", .{cg_root});
    const slurm_dir = try std.fmt.allocPrint(a, "{s}/slurmstepd.scope", .{slice_dir});
    const job_dir = try std.fmt.allocPrint(a, "{s}/job_42", .{slurm_dir});
    const step_dir = try std.fmt.allocPrint(a, "{s}/step_0", .{job_dir});
    const task_dir = try std.fmt.allocPrint(a, "{s}/task_0", .{step_dir});
    for ([_][]const u8{ cg_root, slice_dir, slurm_dir, job_dir, step_dir, task_dir }) |dir| {
        _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, dir)).ptr, 0o700);
    }

    const step_eff = try std.fmt.allocPrint(a, "{s}/cpuset.cpus.effective", .{step_dir});
    const task_eff = try std.fmt.allocPrint(a, "{s}/cpuset.cpus.effective", .{task_dir});
    const proc_cg = try std.fmt.allocPrint(a, "{s}/proc_cgroup", .{base});
    try std.testing.expect(writePrivateNew(step_eff, "2-3\n", 0o600));
    try std.testing.expect(writePrivateNew(task_eff, "\n", 0o600));
    try std.testing.expect(writePrivateNew(proc_cg, "0::/system.slice/slurmstepd.scope/job_42/step_0/task_0\n", 0o600));

    const cg = cgroupCpusFrom(a, proc_cg, cg_root).?;
    try std.testing.expectEqual(@as(usize, 2), cg.count());
    try std.testing.expect(cg.has(2) and cg.has(3));

    const ns_root = try std.fmt.allocPrint(a, "{s}/nscg", .{base});
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, ns_root)).ptr, 0o700);
    const ns_eff = try std.fmt.allocPrint(a, "{s}/cpuset.cpus.effective", .{ns_root});
    const ns_proc = try std.fmt.allocPrint(a, "{s}/ns_proc", .{base});
    try std.testing.expect(writePrivateNew(ns_eff, "4,6\n", 0o600));
    try std.testing.expect(writePrivateNew(ns_proc, "0::/\n", 0o600));
    const ns_cg = cgroupCpusFrom(a, ns_proc, ns_root).?;
    try std.testing.expectEqual(@as(usize, 2), ns_cg.count());
    try std.testing.expect(ns_cg.has(4) and ns_cg.has(6));

    var wide_sched: CpuSet = .{};
    for (0..8) |cpu| wide_sched.add(cpu);
    const bounds = taskCpuBounds(.empty, wide_sched, cg);
    try std.testing.expect(bounds.allowed.?.eql(cg));
    try std.testing.expect(bounds.base_cpus.?.eql(cg));

    var narrow_sched: CpuSet = .{};
    narrow_sched.add(2);
    const narrow = taskCpuBounds(.empty, narrow_sched, cg);
    try std.testing.expect(narrow.base_cpus.?.eql(cg));
    try std.testing.expect(narrow.allowed.?.eql(narrow_sched));

    const lxcfs_narrow =
        \\processor : 0
        \\vendor_id : GenuineIntel
        \\flags : avx2 fma
        \\
        \\processor : 1
        \\vendor_id : GenuineIntel
        \\flags : avx2 fma avx512f waitpkg
        \\
    ;
    var cgroup_4_5: CpuSet = .{};
    cgroup_4_5.add(4);
    cgroup_4_5.add(5);
    var affinity_4: CpuSet = .{};
    affinity_4.add(4);
    const remap_bounds = taskCpuBounds(.empty, affinity_4, cgroup_4_5);
    const scoped = scopeCpuinfoWithBase(a, lxcfs_narrow, remap_bounds.allowed.?, remap_bounds.base_cpus).?;
    try std.testing.expect(std.mem.find(u8, scoped, "flags : avx2 fma\n") != null);
    try std.testing.expect(std.mem.find(u8, scoped, "waitpkg") == null);
    try std.testing.expect(std.mem.find(u8, scoped, "processor : 0\nflags :\n") == null);
}

fn makeDirs(path: []const u8) void {
    var buf: [sys.path_max:0]u8 = undefined;
    var i: usize = 1;
    while (i <= path.len) : (i += 1) {
        if (i != path.len and path[i] != '/') continue;
        const z = sys.z(&buf, path[0..i]) catch return;
        _ = linux.mkdirat(fsx.cwd, z.ptr, 0o755);
    }
}

fn writeUnder(arena: Allocator, path: []const u8, data: []const u8) !void {
    _ = arena;
    const slash = std.mem.lastIndexOfScalar(u8, path, '/') orelse return error.TestUnexpectedResult;
    makeDirs(path[0..slash]);
    try std.testing.expect(writePrivateNew(path, data, 0o644));
}

fn expectCpus(set: ?CpuSet, ids: []const usize) !void {
    const found = set orelse return error.TestExpectedEqual;
    var cpu: usize = 0;
    while (cpu < cpu_id_max) : (cpu += 1) {
        var want = false;
        for (ids) |id| {
            if (id == cpu) want = true;
        }
        try std.testing.expectEqual(want, found.has(cpu));
    }
}

fn cgroupFixture(arena: Allocator, name: []const u8) ![]const u8 {
    var cwd_buf: [sys.path_max]u8 = undefined;
    const here = try fsx.realpath(fsx.cwd, ".", &cwd_buf);
    const base = try std.fmt.allocPrint(arena, "{s}/.zig-cache/eag-cgv1-{s}-{d}", .{ here, name, linux.getpid() });
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.mkdirat(fsx.cwd, (try sys.z(&zb, base)).ptr, 0o700);
    return base;
}

test "mountinfo unescape keeps invalid octal literal" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const unescaped = unescapeMountinfo(
        arena_state.allocator(),
        "/bad\\089/high\\777/good\\040path",
    ).?;
    try std.testing.expectEqualStrings(
        "/bad\\089/high\\777/good path",
        unescaped,
    );
}

test "root cpuset walk does not read relative to cwd" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const relative = "cpuset.cpus.effective";
    try std.testing.expect(writePrivateNew(relative, "7\n", 0o600));
    defer unlinkPath(relative);
    try std.testing.expect(walkCpuset(arena_state.allocator(), "/", "/") == null);
}

test "cgroup v1 cpuset mounted off the default path" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try cgroupFixture(a, "off");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const proc_dir = try std.fmt.allocPrint(a, "{s}/proc/self", .{base});
    const cg = try std.fmt.allocPrint(a, "{s}/sys/fs/cgroup", .{base});
    const point = try std.fmt.allocPrint(a, "{s}/cgroup/cpuset", .{base});
    const other = try std.fmt.allocPrint(a, "{s}/cgroup/notcpuset", .{base});
    const proc_file = try std.fmt.allocPrint(a, "{s}/cgroup", .{proc_dir});
    const mountinfo = try std.fmt.allocPrint(a, "{s}/mountinfo", .{proc_dir});
    try writeUnder(a, proc_file, "2:cpuset:/job:42\n");
    const mi = try std.fmt.allocPrint(a, "21 20 0:21 / {s} rw - cgroup cgroup rw,cpusetfoo\n22 20 0:22 / {s} rw - cgroup cgroup rw,cpuset\n", .{ other, point });
    try writeUnder(a, mountinfo, mi);
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/job:42/cpuset.cpus.effective", .{other}), "0-3\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/job:42/cpuset.cpus.effective", .{point}), "4-5\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/cpuset/job:42/cpuset.cpus.effective", .{cg}), "0\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/job:42/cpuset.cpus.effective", .{cg}), "1\n");
    try expectCpus(cgroupCpusFrom(a, proc_file, cg), &.{ 4, 5 });
}

test "nested cgroup stops at the cpuset mount" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try cgroupFixture(a, "nest");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const proc_dir = try std.fmt.allocPrint(a, "{s}/proc/self", .{base});
    const cg = try std.fmt.allocPrint(a, "{s}/sys/fs/cgroup", .{base});
    const point = try std.fmt.allocPrint(a, "{s}/cgroup/cpuset", .{base});
    const proc_file = try std.fmt.allocPrint(a, "{s}/cgroup", .{proc_dir});
    try writeUnder(a, proc_file, "2:cpuset:/a/b/c\n");
    const mi = try std.fmt.allocPrint(a, "22 20 0:22 / {s} rw - cgroup cgroup rw,cpuset\n", .{point});
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/mountinfo", .{proc_dir}), mi);
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/a/cpuset.cpus.effective", .{point}), "6,7\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/cgroup/cpuset.cpus.effective", .{base}), "9\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/cpuset/a/cpuset.cpus.effective", .{cg}), "1\n");
    try expectCpus(cgroupCpusFrom(a, proc_file, cg), &.{ 6, 7 });
}

test "missing cpuset controller is not a cpuset" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try cgroupFixture(a, "miss");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const proc_dir = try std.fmt.allocPrint(a, "{s}/proc/self", .{base});
    const cg = try std.fmt.allocPrint(a, "{s}/sys/fs/cgroup", .{base});
    const cpu = try std.fmt.allocPrint(a, "{s}/cpu,cpuacct", .{cg});
    const proc_file = try std.fmt.allocPrint(a, "{s}/cgroup", .{proc_dir});
    try writeUnder(a, proc_file, "2:cpu,cpuacct:/job\n0::/job\n");
    const mi = try std.fmt.allocPrint(a, "30 20 0:30 / {s} rw - cgroup cgroup rw,cpu,cpuacct\n31 20 0:31 / {s} rw - cgroup2 cgroup2 rw\n", .{ cpu, cg });
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/mountinfo", .{proc_dir}), mi);
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/job/cgroup.controllers", .{cpu}), "cpu mycpuset memory\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/job/cpuset.cpus.effective", .{cpu}), "8\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/job/cgroup.controllers", .{cg}), "cpu mycpuset memory\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/job/cpuset.cpus.effective", .{cg}), "8\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/cgroup.controllers", .{cg}), "cpu mycpuset memory\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/cpuset.cpus.effective", .{cg}), "9\n");
    try std.testing.expect(cgroupCpusFrom(a, proc_file, cg) == null);
}

test "dotdot mount root keeps the cgroup root" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try cgroupFixture(a, "dot");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const proc_dir = try std.fmt.allocPrint(a, "{s}/proc/self", .{base});
    const cg = try std.fmt.allocPrint(a, "{s}/sys/fs/cgroup", .{base});
    const outside = try std.fmt.allocPrint(a, "{s}/outside", .{base});
    const proc_file = try std.fmt.allocPrint(a, "{s}/cgroup", .{proc_dir});
    try writeUnder(a, proc_file, "0::/../job\n");
    const mi = try std.fmt.allocPrint(a, "40 20 0:40 /.. {s} rw - cgroup2 cgroup2 rw\n", .{outside});
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/mountinfo", .{proc_dir}), mi);
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/job/cpuset.cpus.effective", .{outside}), "9\n");
    try writeUnder(a, try std.fmt.allocPrint(a, "{s}/cpuset.cpus.effective", .{cg}), "0-3\n");
    try expectCpus(cgroupCpusFrom(a, proc_file, cg), &.{ 0, 1, 2, 3 });
}

test "parseCpuSpec rejects trailing commas" {
    try std.testing.expect(parseCpuSpec("0,") == null);
    try std.testing.expect(parseCpuSpec("0-1,") == null);
    try std.testing.expect(parseCpuSpec("0, ") == null);
    try std.testing.expect(parseCpuSpec("cpu[0,]") == null);
    try std.testing.expect(parseCpuSpec("0 1") == null);
    try std.testing.expect(parseCpuSpec("0 - 1") == null);
    try std.testing.expect(parseCpuSpec("0,1") != null);
    try std.testing.expect(parseCpuSpec("cpu[0, 1]") != null);
}

test "parseCpuSpec unions a mask_cpu list" {
    try expectCpus(parseCpuSpec("0x2"), &.{1});
    try expectCpus(parseCpuSpec("0x2,0x4"), &.{ 1, 2 });
    try expectCpus(parseCpuSpec("0x2,0x2"), &.{1});
    try expectCpus(parseCpuSpec("0X2, 0x4"), &.{ 1, 2 });
    try expectCpus(parseCpuSpec("0x0,0x2"), &.{1});
    try std.testing.expect(parseCpuSpec("0x1,") == null);
    try std.testing.expect(parseCpuSpec("0x1,0x") == null);
    try std.testing.expect(parseCpuSpec("0x1,2") == null);
    var high: [2 + 1 + 1024]u8 = undefined;
    high[0] = '0';
    high[1] = 'x';
    high[2] = '1';
    @memset(high[3..], '0');
    try std.testing.expect(parseCpuSpec(&high) == null);
    var listed: [4 + high.len]u8 = undefined;
    @memcpy(listed[0..4], "0x2,");
    @memcpy(listed[4..], &high);
    try std.testing.expect(parseCpuSpec(&listed) == null);
}

test "resolvePath searches default_path and empty colon entries" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const empty_env: Map = .empty;
    try std.testing.expect(resolvePath(a, "sh", empty_env) != null);

    var colon_env: Map = .empty;
    try colon_env.put(a, "PATH", ":/bin");
    try std.testing.expect(resolvePath(a, "sh", colon_env) != null);

    var c: Ctx = .{ .arena = a, .env = empty_env, .strict = false, .self_exe = "" };
    const rel = resolve(&c, "rel/tool", empty_env).?;
    try std.testing.expect(std.mem.endsWith(u8, rel, "/rel/tool"));
}
