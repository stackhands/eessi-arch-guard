//! Best-effort RFC 5424 syslog and native systemd-journald decision emitter.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const text = @import("text.zig");
const envmap = @import("envmap.zig");
const guard = @import("guard.zig");

const Allocator = std.mem.Allocator;

/// The transports, as zig-common names them.
pub const Scheme = common.syslog.Scheme;

pub const Target = struct {
    scheme: Scheme,
    host_or_path: []const u8,
    port: u16 = 0,
};

/// IANA documentation Private Enterprise Number (RFC 5398 / RFC 5424 section 6.3.2),
/// used as the default SD-ID suffix until a site or project PEN is assigned.
pub const default_pen = "32473";

pub const Resolved = struct {
    target: ?Target = null,
    log_all: bool = false,
    pen: []const u8 = default_pen,
    target_warning: ?[]const u8 = null,
    pen_warning: ?[]const u8 = null,
};

const Param = struct {
    key: []const u8,
    value: []const u8,
};

fn splitHostPort(addr: []const u8) ?struct { host: []const u8, port: u16 } {
    if (addr.len == 0) return null;
    var host: []const u8 = "";
    var port_s: []const u8 = "";
    if (addr[0] == '[') {
        const end = std.mem.findScalar(u8, addr, ']') orelse return null;
        if (end + 2 > addr.len or addr[end + 1] != ':') return null;
        host = addr[1..end];
        port_s = addr[end + 2 ..];
    } else {
        const colon = std.mem.findScalarLast(u8, addr, ':') orelse return null;
        host = addr[0..colon];
        port_s = addr[colon + 1 ..];
    }
    if (host.len == 0 or port_s.len == 0) return null;
    for (port_s) |c| if (!std.ascii.isDigit(c)) return null;
    const port = std.fmt.parseInt(u16, port_s, 10) catch return null;
    if (port == 0) return null;
    return .{ .host = host, .port = port };
}

pub fn parseTarget(raw: []const u8) ?Target {
    const s = text.pyStrip(raw);
    if (s.len == 0 or text.hasLineBreakOrNul(s)) return null;
    if (std.mem.eql(u8, s, "syslog")) return .{ .scheme = .rfc5424_unix, .host_or_path = "/dev/log" };
    if (std.mem.eql(u8, s, "journald") or std.mem.eql(u8, s, "journal"))
        return .{ .scheme = .journal_unix, .host_or_path = "/run/systemd/journal/socket" };
    if (s[0] == '/') return .{ .scheme = .rfc5424_unix, .host_or_path = s };
    if (std.mem.startsWith(u8, s, "unix://")) {
        const path = s["unix://".len..];
        if (path.len == 0 or path[0] != '/') return null;
        return .{ .scheme = .rfc5424_unix, .host_or_path = path };
    }
    if (std.mem.startsWith(u8, s, "journal+unix://")) {
        const path = s["journal+unix://".len..];
        if (path.len == 0 or path[0] != '/') return null;
        return .{ .scheme = .journal_unix, .host_or_path = path };
    }
    if (std.mem.startsWith(u8, s, "udp://")) {
        const hp = splitHostPort(s["udp://".len..]) orelse return null;
        return .{ .scheme = .rfc5424_udp, .host_or_path = hp.host, .port = hp.port };
    }
    if (std.mem.startsWith(u8, s, "tcp://")) {
        const hp = splitHostPort(s["tcp://".len..]) orelse return null;
        return .{ .scheme = .rfc5424_tcp, .host_or_path = hp.host, .port = hp.port };
    }
    return null;
}

pub fn parsePen(arena: Allocator, raw: []const u8) ?[]const u8 {
    const s = text.pyStrip(raw);
    if (s.len == 0 or s.len > 20) return null;
    for (s) |c| if (c < '0' or c > '9') return null;
    const val = std.fmt.parseInt(u128, s, 10) catch return null;
    if (val == 0) return null;
    return arena.print("{d}", .{val}) catch null;
}

/// Canonical string representation of a parsed syslog target.
pub fn formatTarget(arena: Allocator, target: ?Target) ?[]const u8 {
    const t = target orelse return null;
    return switch (t.scheme) {
        .rfc5424_unix => arena.print("unix://{s}", .{t.host_or_path}) catch null,
        .journal_unix => arena.print("journal+unix://{s}", .{t.host_or_path}) catch null,
        .rfc5424_udp => if (std.mem.findScalar(u8, t.host_or_path, ':') != null)
            arena.print("udp://[{s}]:{d}", .{ t.host_or_path, t.port }) catch null
        else
            arena.print("udp://{s}:{d}", .{ t.host_or_path, t.port }) catch null,
        .rfc5424_tcp => if (std.mem.findScalar(u8, t.host_or_path, ':') != null)
            arena.print("tcp://[{s}]:{d}", .{ t.host_or_path, t.port }) catch null
        else
            arena.print("tcp://{s}:{d}", .{ t.host_or_path, t.port }) catch null,
    };
}

pub fn resolve(
    arena: Allocator,
    cli_text: ?[]const u8,
    env: envmap.Map,
    settings: ?std.StringArrayHashMapUnmanaged([]const u8),
) Resolved {
    var raw: ?[]const u8 = null;
    var source: []const u8 = "syslog";
    if (cli_text) |c| {
        raw = c;
        source = "--syslog";
    } else if (env.get("EESSI_ARCH_GUARD_SYSLOG")) |ev| if (ev.len > 0) {
        raw = ev;
        source = "EESSI_ARCH_GUARD_SYSLOG";
    };
    // An empty environment value is unset, as an empty config value is.
    if (raw == null) if (settings) |st| if (st.get("syslog")) |cv| if (cv.len > 0) {
        raw = cv;
        source = "config syslog";
    };

    var log_all = std.mem.eql(u8, env.get("EESSI_ARCH_GUARD_SYSLOG_ALL") orelse "", "1");
    if (!log_all) if (settings) |st| if (st.get("syslog_all")) |v| {
        if (std.mem.eql(u8, v, "1") or std.mem.eql(u8, v, "true") or std.mem.eql(u8, v, "yes")) {
            log_all = true;
        }
    };

    var pen_warnings: std.ArrayList([]const u8) = .empty;
    var pen: []const u8 = default_pen;
    if (settings) |st| if (st.get("syslog_pen")) |cv| if (cv.len > 0) {
        if (parsePen(arena, cv)) |p| {
            pen = p;
        } else {
            const shown = text.oneLine(arena, cv, 64) catch cv;
            if (arena.print("eessi-arch-guard: config syslog_pen is not a positive integer: {s}", .{shown}) catch null) |w| {
                pen_warnings.append(arena, w) catch {};
            }
        }
    };
    if (env.get("EESSI_ARCH_GUARD_SYSLOG_PEN")) |ev| if (ev.len > 0) {
        if (parsePen(arena, ev)) |p| {
            pen = p;
        } else {
            const shown = text.oneLine(arena, ev, 64) catch ev;
            if (arena.print("eessi-arch-guard: EESSI_ARCH_GUARD_SYSLOG_PEN is not a positive integer: {s}", .{shown}) catch null) |w| {
                pen_warnings.append(arena, w) catch {};
            }
        }
    };

    var target: ?Target = null;
    var target_warning: ?[]const u8 = null;
    if (raw) |spec| if (spec.len > 0) {
        target = parseTarget(spec) orelse blk: {
            const shown = text.oneLine(arena, spec, 120) catch spec;
            target_warning = arena.print("eessi-arch-guard: {s} is not a valid syslog target: {s}", .{ source, shown }) catch null;
            break :blk null;
        };
    };

    const pen_warning: ?[]const u8 = if (pen_warnings.items.len == 0) null else (std.mem.join(arena, " ", pen_warnings.items) catch null);
    return .{
        .target = target,
        .log_all = log_all,
        .pen = pen,
        .target_warning = target_warning,
        .pen_warning = pen_warning,
    };
}

fn sdEscape(arena: Allocator, value: []const u8) Allocator.Error![]u8 {
    const clean = try text.oneLine(arena, value, 256);
    var out: std.ArrayList(u8) = .empty;
    for (clean) |ch| {
        if (ch == '\\' or ch == '"' or ch == ']') try out.append(arena, '\\');
        try out.append(arena, ch);
    }
    return out.items;
}

fn rfc5424Token(arena: Allocator, value: []const u8) Allocator.Error![]const u8 {
    var out: std.ArrayList(u8) = .empty;
    for (value) |ch| {
        if (ch >= 33 and ch <= 126) {
            try out.append(arena, ch);
            if (out.items.len == 255) break;
        }
    }
    if (out.items.len == 0) return "-";
    return out.items;
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

fn hostname(arena: Allocator) []const u8 {
    var uts: linux.utsname = undefined;
    if (linux.errno(linux.uname(&uts)) != .SUCCESS) return "-";
    const raw = std.mem.sliceTo(&uts.nodename, 0);
    return rfc5424Token(arena, raw) catch "-";
}

fn utcTimestamp(arena: Allocator) []const u8 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(.REALTIME, &ts);
    const secs: u64 = if (ts.sec > 0) @intCast(ts.sec) else 0;
    const usec: u32 = if (ts.nsec >= 0) @intCast(@divTrunc(ts.nsec, 1000)) else 0;
    const es = std.time.epoch.EpochSeconds{ .secs = secs };
    const day = es.getEpochDay().calculateYearDay();
    const md = day.calculateMonthDay();
    const ds = es.getDaySeconds();
    return arena.print("{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}.{d:0>6}Z", .{
        day.year,
        md.month.numeric(),
        md.day_index + 1,
        ds.getHoursIntoDay(),
        ds.getMinutesIntoHour(),
        ds.getSecondsIntoMinute(),
        usec,
    }) catch "1970-01-01T00:00:00.000000Z";
}

const BuiltEvent = struct {
    severity: u8,
    msgid: []const u8,
    params: []const Param,
    msg: []const u8,
};

fn buildEvent(
    arena: Allocator,
    outcome: guard.Outcome,
    env: envmap.Map,
    cached: bool,
    strict: bool,
) Allocator.Error!BuiltEvent {
    var was = env.get("EESSI_SOFTWARE_SUBDIR") orelse "";
    var now: []const u8 = "";
    var cause: []const u8 = "";
    var severity: u8 = 6;
    var msgid: []const u8 = "EESSI_ARCH_NOOP";
    var raw_msg: []const u8 = "";

    switch (outcome.kind) {
        .reloaded => {
            severity = 5;
            msgid = "EESSI_ARCH_RELOAD";
            const marker = (outcome.changes.get("EESSI_ARCH_GUARD_RELOADED") orelse null) orelse "";
            if (splitArrow(marker)) |pair| {
                was = pair.lhs;
                now = pair.rhs;
            } else {
                now = (outcome.changes.get("EESSI_SOFTWARE_SUBDIR") orelse null) orelse "";
            }
            raw_msg = outcome.message orelse (try arena.print("reloaded {s}->{s}", .{ was, now }));
        },
        .failed => {
            severity = 3;
            msgid = "EESSI_ARCH_FAIL";
            const marker = (outcome.changes.get("EESSI_ARCH_GUARD_FAILED") orelse null) orelse "";
            if (splitArrow(marker)) |pair| {
                was = pair.lhs;
                cause = pair.rhs;
            } else {
                cause = "failed";
            }
            raw_msg = outcome.message orelse "eessi-arch-guard failed";
        },
        .skip => {
            severity = 6;
            msgid = "EESSI_ARCH_SKIP";
            cause = skipCause(outcome.detail);
            raw_msg = outcome.detail orelse "eessi-arch-guard skipped";
        },
        .noop => {
            severity = 6;
            msgid = "EESSI_ARCH_NOOP";
            now = was;
            raw_msg = outcome.detail orelse "target already matches this node";
        },
    }

    var params: std.ArrayList(Param) = .empty;
    try params.appendSlice(arena, &.{
        .{ .key = "kind", .value = switch (outcome.kind) {
            .skip => "skip",
            .noop => "noop",
            .reloaded => "reloaded",
            .failed => "failed",
        } },
        .{ .key = "cached", .value = if (cached) "1" else "0" },
        .{ .key = "strict", .value = if (strict) "1" else "0" },
    });
    if (env.get("SLURM_JOB_ID")) |v| if (v.len > 0) try params.append(arena, .{ .key = "job", .value = v });
    if (env.get("SLURM_STEP_ID")) |v| if (v.len > 0) try params.append(arena, .{ .key = "step", .value = v });
    if (env.get("SLURM_LOCALID")) |v| if (v.len > 0) try params.append(arena, .{ .key = "localid", .value = v });
    if (was.len > 0) try params.append(arena, .{ .key = "was", .value = was });
    if (now.len > 0) try params.append(arena, .{ .key = "now", .value = now });
    if (cause.len > 0) try params.append(arena, .{ .key = "cause", .value = cause });

    return .{
        .severity = severity,
        .msgid = msgid,
        .params = params.items,
        .msg = try text.oneLine(arena, raw_msg, 500),
    };
}

fn utf8ReplaceBytes(arena: Allocator, s: []const u8) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    var i: usize = 0;
    while (i < s.len) {
        const ch = text.nextChar(s[i..]);
        if (ch.cp == null) {
            try out.append(arena, '?');
        } else {
            try out.appendSlice(arena, s[i .. i + ch.len]);
        }
        i += ch.len;
    }
    return out.items;
}

fn formatRfc5424(arena: Allocator, ev: BuiltEvent, pen: []const u8) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    const pri: u8 = 8 + ev.severity;
    const ts = utcTimestamp(arena);
    const host = hostname(arena);
    const pid = linux.getpid();
    try out.print(arena, "<{d}>1 {s} {s} eessi-arch-guard {d} {s} [archGuard@{s}", .{
        pri, ts, host, pid, ev.msgid, pen,
    });
    for (ev.params) |p| {
        const esc = try sdEscape(arena, p.value);
        try out.print(arena, " {s}=\"{s}\"", .{ p.key, esc });
    }
    try out.print(arena, "] {s}", .{ev.msg});
    return utf8ReplaceBytes(arena, out.items);
}

fn journalKv(arena: Allocator, out: *std.ArrayList(u8), key: []const u8, value: []const u8) Allocator.Error!void {
    const clean_val = try utf8ReplaceBytes(arena, value);
    if (std.mem.findAny(u8, clean_val, "\n\x00") != null) {
        try out.appendSlice(arena, key);
        try out.append(arena, '\n');
        var len_buf: [8]u8 = undefined;
        std.mem.writeInt(u64, &len_buf, clean_val.len, .little);
        try out.appendSlice(arena, &len_buf);
        try out.appendSlice(arena, clean_val);
        try out.append(arena, '\n');
    } else {
        try out.print(arena, "{s}={s}\n", .{ key, clean_val });
    }
}

fn journalFieldFor(key: []const u8) ?[]const u8 {
    if (std.mem.eql(u8, key, "kind")) return "ARCH_GUARD_KIND";
    if (std.mem.eql(u8, key, "cached")) return "ARCH_GUARD_CACHED";
    if (std.mem.eql(u8, key, "strict")) return "ARCH_GUARD_STRICT";
    if (std.mem.eql(u8, key, "was")) return "ARCH_GUARD_WAS";
    if (std.mem.eql(u8, key, "now")) return "ARCH_GUARD_NOW";
    if (std.mem.eql(u8, key, "cause")) return "ARCH_GUARD_CAUSE";
    if (std.mem.eql(u8, key, "job")) return "SLURM_JOB_ID";
    if (std.mem.eql(u8, key, "step")) return "SLURM_STEP_ID";
    if (std.mem.eql(u8, key, "localid")) return "SLURM_LOCALID";
    return null;
}

fn formatJournald(arena: Allocator, ev: BuiltEvent) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    var sev_buf: [4]u8 = undefined;
    const sev_s = std.fmt.bufPrint(&sev_buf, "{d}", .{ev.severity}) catch "6";
    try journalKv(arena, &out, "MESSAGE", ev.msg);
    try journalKv(arena, &out, "PRIORITY", sev_s);
    try journalKv(arena, &out, "SYSLOG_IDENTIFIER", "eessi-arch-guard");
    try journalKv(arena, &out, "SYSLOG_FACILITY", "1");
    try journalKv(arena, &out, "MSGID", ev.msgid);
    for (ev.params) |p| {
        if (journalFieldFor(p.key)) |field| {
            const clean = try text.oneLine(arena, p.value, 500);
            try journalKv(arena, &out, field, clean);
        }
    }
    return out.items;
}

pub fn emitDecision(
    arena: Allocator,
    resolved: Resolved,
    outcome: guard.Outcome,
    env: envmap.Map,
    cached: bool,
    strict: bool,
) void {
    const target = resolved.target orelse return;
    if ((outcome.kind == .skip or outcome.kind == .noop) and !resolved.log_all) return;
    const ev = buildEvent(arena, outcome, env, cached, strict) catch return;
    const payload = switch (target.scheme) {
        .journal_unix => formatJournald(arena, ev),
        else => formatRfc5424(arena, ev, resolved.pen),
    } catch return;
    // The transport is zig-common's: a unix syslog socket as a datagram and
    // then as a stream, UDP, or TCP with octet counting. Best effort.
    _ = common.syslog.send(arena, .{
        .scheme = target.scheme,
        .host_or_path = target.host_or_path,
        .port = target.port,
    }, payload);
}

pub fn emitEmergency(
    arena: Allocator,
    resolved: Resolved,
    env: envmap.Map,
    kind: []const u8,
    detail: []const u8,
    strict: bool,
) void {
    const target = resolved.target orelse return;
    var ch: envmap.Changes = .empty;
    const was = env.get("EESSI_SOFTWARE_SUBDIR") orelse "";
    const was_side = if (was.len > 0 and !text.hasLineBreakOrNul(was)) was else "unrecognized";
    const token = arena.print("{s}->{s}", .{ was_side, kind }) catch "unrecognized->error";
    ch.put(arena, "EESSI_ARCH_GUARD_FAILED", token) catch {};
    const msg = arena.print("eessi-arch-guard: skipped, {s}: {s}", .{ kind, detail }) catch "eessi-arch-guard: skipped";
    emitDecision(arena, .{ .target = target, .log_all = resolved.log_all, .pen = resolved.pen }, .{
        .changes = ch,
        .message = msg,
        .kind = .failed,
    }, env, false, strict);
}

test "parseTarget accepts unix, journald, udp and tcp" {
    try std.testing.expectEqual(Scheme.rfc5424_unix, parseTarget("syslog").?.scheme);
    try std.testing.expectEqualStrings("/dev/log", parseTarget("syslog").?.host_or_path);
    try std.testing.expectEqual(Scheme.journal_unix, parseTarget("journald").?.scheme);
    try std.testing.expectEqualStrings("/run/systemd/journal/socket", parseTarget("journal").?.host_or_path);
    try std.testing.expectEqual(Scheme.rfc5424_unix, parseTarget("unix:///tmp/s.sock").?.scheme);
    try std.testing.expectEqual(Scheme.journal_unix, parseTarget("journal+unix:///tmp/j.sock").?.scheme);
    const u = parseTarget("udp://127.0.0.1:5140").?;
    try std.testing.expectEqual(Scheme.rfc5424_udp, u.scheme);
    try std.testing.expectEqualStrings("127.0.0.1", u.host_or_path);
    try std.testing.expectEqual(@as(u16, 5140), u.port);
    const t = parseTarget("tcp://[::1]:1514").?;
    try std.testing.expectEqual(Scheme.rfc5424_tcp, t.scheme);
    try std.testing.expectEqualStrings("::1", t.host_or_path);
    try std.testing.expectEqual(@as(u16, 1514), t.port);
    try std.testing.expect(parseTarget("bogus://x") == null);
    try std.testing.expect(parseTarget("tcp://host:0") == null);
    try std.testing.expect(parseTarget("unix://relative") == null);
}

test "formatRfc5424 escapes structured data values and uses configured PEN" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const msg = try formatRfc5424(a, .{
        .severity = 5,
        .msgid = "EESSI_ARCH_RELOAD",
        .params = &.{
            .{ .key = "kind", .value = "reloaded" },
            .{ .key = "cause", .value = "a\"b\\c]d" },
        },
        .msg = "hello",
    }, "55555");
    try std.testing.expect(std.mem.startsWith(u8, msg, "<13>1 "));
    try std.testing.expect(std.mem.find(u8, msg, "eessi-arch-guard ") != null);
    try std.testing.expect(std.mem.find(u8, msg, "EESSI_ARCH_RELOAD [archGuard@55555 kind=\"reloaded\" cause=\"a\\\"b\\\\c\\]d\"] hello") != null);
    try std.testing.expect(parsePen(a, "0") == null);
    try std.testing.expect(parsePen(a, "abc") == null);
    try std.testing.expectEqualStrings("49152", parsePen(a, "049152").?);
    try std.testing.expectEqualStrings("18446744073709551616", parsePen(a, "18446744073709551616").?);
    try std.testing.expectEqualStrings("99999999999999999999", parsePen(a, "99999999999999999999").?);
    try std.testing.expect(parsePen(a, "100000000000000000000") == null);
}

test "formatTarget preserves brackets around IPv6 hosts" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    try std.testing.expectEqualStrings("tcp://[::1]:514", formatTarget(a, parseTarget("tcp://[::1]:514")).?);
    try std.testing.expectEqualStrings("udp://[2001:db8::1]:1514", formatTarget(a, parseTarget("udp://[2001:db8::1]:1514")).?);
    try std.testing.expectEqualStrings("udp://127.0.0.1:514", formatTarget(a, parseTarget("udp://127.0.0.1:514")).?);
}

test "formatRfc5424 and formatJournald replace invalid UTF-8 bytes" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const ev: BuiltEvent = .{
        .severity = 6,
        .msgid = "EESSI_ARCH_NOOP",
        .params = &.{.{ .key = "job", .value = "77\xff" }},
        .msg = "bad\xffmsg",
    };
    const rfc = try formatRfc5424(a, ev, default_pen);
    try std.testing.expect(std.unicode.utf8ValidateSlice(rfc));
    try std.testing.expect(std.mem.find(u8, rfc, "job=\"77?\"") != null);
    try std.testing.expect(std.mem.find(u8, rfc, "bad?msg") != null);

    const jrn = try formatJournald(a, ev);
    try std.testing.expect(std.unicode.utf8ValidateSlice(jrn));
    try std.testing.expect(std.mem.find(u8, jrn, "SLURM_JOB_ID=77?\n") != null);
    try std.testing.expect(std.mem.find(u8, jrn, "MESSAGE=bad?msg\n") != null);
}

test "resolve separates target_warning and pen_warning" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var env: envmap.Map = .empty;
    try env.put(a, "EESSI_ARCH_GUARD_SYSLOG_PEN", "bad-pen");
    const r = resolve(a, "bad-target", env, null);
    try std.testing.expect(r.target == null);
    try std.testing.expectEqualStrings(default_pen, r.pen);
    try std.testing.expectEqualStrings("eessi-arch-guard: --syslog is not a valid syslog target: bad-target", r.target_warning.?);
    try std.testing.expectEqualStrings("eessi-arch-guard: EESSI_ARCH_GUARD_SYSLOG_PEN is not a positive integer: bad-pen", r.pen_warning.?);
}

test "an empty syslog environment value keeps the site config target" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var settings: std.StringArrayHashMapUnmanaged([]const u8) = .empty;
    try settings.put(a, "syslog", "journald");

    var env: envmap.Map = .empty;
    try env.put(a, "EESSI_ARCH_GUARD_SYSLOG", "");
    const from_config = resolve(a, null, env, settings);
    try std.testing.expect(from_config.target_warning == null);
    try std.testing.expect(from_config.target != null);
    try std.testing.expectEqual(Scheme.journal_unix, from_config.target.?.scheme);
    try std.testing.expectEqualStrings("/run/systemd/journal/socket", from_config.target.?.host_or_path);

    try env.put(a, "EESSI_ARCH_GUARD_SYSLOG", "syslog");
    const from_env = resolve(a, null, env, settings);
    try std.testing.expectEqual(Scheme.rfc5424_unix, from_env.target.?.scheme);
    try std.testing.expectEqualStrings("/dev/log", from_env.target.?.host_or_path);
}
