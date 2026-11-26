//! Tests of how the guard scopes cpuinfo to the processors a task may use:
//! the bound processors, LXCFS renumbering, cgroup v1 and v2 cpusets, the
//! waitpkg march rule, and the digest and directory of the decision cache.
//! The cpuinfo files come from tests/fixtures/cpuinfo, read relative to the
//! build root, where `zig build test` runs.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const guard = @import("guard.zig");
const envmap = @import("envmap.zig");
const text = @import("text.zig");

const sys = common.sys;
const fsx = common.fsx;
const CpuSet = guard.CpuSet;
const Allocator = std.mem.Allocator;

const fixtures = "tests/fixtures/cpuinfo/";

fn readFixture(a: Allocator, name: []const u8) ![]const u8 {
    const path = try std.fmt.allocPrint(a, "{s}{s}", .{ fixtures, name });
    const f = try fsx.openRegular(fsx.cwd, path, true);
    defer sys.close(f.fd);
    return fsx.readAll(a, f.fd, 1 << 20);
}

fn replaceFirst(a: Allocator, hay: []const u8, old: []const u8, new: []const u8) ![]const u8 {
    const at = std.mem.find(u8, hay, old) orelse return hay;
    return std.mem.concat(a, u8, &.{ hay[0..at], new, hay[at + old.len ..] });
}

fn setOf(ids: []const usize) CpuSet {
    var s: CpuSet = .{};
    for (ids) |id| s.add(id);
    return s;
}

fn contains(hay: []const u8, needle: []const u8) bool {
    return std.mem.find(u8, hay, needle) != null;
}

/// A scratch directory under .zig-cache, removed by the caller.
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

fn makeDirs(path: []const u8) void {
    var buf: [sys.path_max:0]u8 = undefined;
    var i: usize = 1;
    while (i <= path.len) : (i += 1) {
        if (i != path.len and path[i] != '/') continue;
        const z = sys.z(&buf, path[0..i]) catch return;
        _ = linux.mkdirat(fsx.cwd, z.ptr, 0o755);
    }
}

fn writeUnder(path: []const u8, data: []const u8) !void {
    const slash = std.mem.lastIndexOfScalar(u8, path, '/') orelse return error.TestUnexpectedResult;
    makeDirs(path[0..slash]);
    try writeFile(path, data);
}

const two_blocks =
    \\processor : 0
    \\vendor_id : GenuineIntel
    \\cpu family : 6
    \\model : 60
    \\flags : avx2 fma
    \\
    \\processor : 1
    \\vendor_id : GenuineIntel
    \\cpu family : 6
    \\model : 143
    \\flags : avx2 fma avx512f waitpkg
    \\
;

test "scoping keeps the bound processor and intersects the flags of several" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    const one = guard.scopeCpuinfo(a, two_blocks, setOf(&.{1})).?;
    try std.testing.expect(contains(one, "model : 143"));
    try std.testing.expect(contains(one, "waitpkg"));
    try std.testing.expect(!contains(one, "model : 60"));

    const both = guard.scopeCpuinfo(a, two_blocks, setOf(&.{ 0, 1 })).?;
    try std.testing.expect(!contains(both, "waitpkg"));
    try std.testing.expect(contains(both, "avx2"));
    try std.testing.expect(!contains(both, "model :"));

    const single = "processor : 0\nvendor_id : GenuineIntel\nflags : avx2 fma\n";
    try std.testing.expect(guard.scopeCpuinfo(a, single, setOf(&.{ 0, 1, 2 })) == null);
    try std.testing.expect(guard.scopeCpuinfo(a, "eessi_arch_matches: x86_64/generic\n", setOf(&.{0})) == null);
}

test "LXCFS renumbered blocks map to the cpuset and gapped logical IDs stay" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const spr_real = try readFixture(a, "sapphirerapids-8488C.cpuinfo");
    const spr_kvm = try readFixture(a, "sapphirerapids-kvm-no-waitpkg.cpuinfo");
    const emr_text = try readFixture(a, "emeraldrapids-kvm-no-waitpkg.cpuinfo");

    var kvm = try replaceFirst(a, spr_kvm, "core id         : 0", "core id         : 1");
    kvm = try replaceFirst(a, kvm, "apicid          : 0", "apicid          : 1");
    kvm = try replaceFirst(a, kvm, "initial apicid  : 0", "initial apicid  : 1");
    var real = try replaceFirst(a, spr_real, "processor       : 0", "processor       : 1");
    real = try replaceFirst(a, real, "core id         : 0", "core id         : 3");
    real = try replaceFirst(a, real, "apicid          : 0", "apicid          : 3");
    real = try replaceFirst(a, real, "initial apicid  : 0", "initial apicid  : 3");
    const lxcfs = try std.mem.concat(a, u8, &.{ kvm, "\n", real });

    const both = guard.scopeCpuinfoWithBase(a, lxcfs, setOf(&.{ 1, 3 }), setOf(&.{ 1, 3 })).?;
    try std.testing.expect(contains(both, "avx512_bf16"));
    try std.testing.expect(!contains(both, "waitpkg"));

    const only_3 = guard.scopeCpuinfoWithBase(a, lxcfs, setOf(&.{3}), setOf(&.{ 1, 3 })).?;
    try std.testing.expect(contains(only_3, "waitpkg"));

    var gap: std.ArrayList(u8) = .empty;
    var blocks = std.mem.splitSequence(u8, std.mem.trim(u8, emr_text, "\n"), "\n\n");
    var kept: usize = 0;
    while (blocks.next()) |block| {
        const first = block[0 .. std.mem.findScalar(u8, block, '\n') orelse block.len];
        const id = std.mem.trim(u8, first[std.mem.findScalarLast(u8, first, ':').? + 1 ..], " \t");
        if (!(std.mem.eql(u8, id, "0") or std.mem.eql(u8, id, "2") or std.mem.eql(u8, id, "4") or std.mem.eql(u8, id, "6"))) continue;
        if (kept > 0) try gap.appendSlice(a, "\n\n");
        try gap.appendSlice(a, block);
        kept += 1;
    }
    try gap.append(a, '\n');
    try std.testing.expectEqual(@as(usize, 4), kept);
    const scoped = guard.scopeCpuinfoWithBase(a, gap.items, setOf(&.{ 4, 6 }), setOf(&.{ 4, 6 })).?;
    try std.testing.expect(contains(scoped, "processor : 4\n"));
    try std.testing.expect(contains(scoped, "amx_tile"));
    try std.testing.expect(!contains(scoped, "waitpkg"));
}

test "a cpuset narrower than the processors of a node picks the candidate without waitpkg" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const spr_real = try readFixture(a, "sapphirerapids-8488C.cpuinfo");
    const spr_kvm = try readFixture(a, "sapphirerapids-kvm-no-waitpkg.cpuinfo");

    var kvm = try replaceFirst(a, spr_kvm, "core id         : 0", "core id         : 4");
    kvm = try replaceFirst(a, kvm, "apicid          : 0", "apicid          : 4");
    kvm = try replaceFirst(a, kvm, "initial apicid  : 0", "initial apicid  : 4");
    var real = try replaceFirst(a, spr_real, "processor       : 0", "processor       : 1");
    real = try replaceFirst(a, real, "core id         : 0", "core id         : 5");
    real = try replaceFirst(a, real, "apicid          : 0", "apicid          : 5");
    real = try replaceFirst(a, real, "initial apicid  : 0", "initial apicid  : 5");
    const joined = try std.mem.concat(a, u8, &.{ kvm, "\n", real });

    // sched affinity {4} inside a cgroup {4, 5}: the cgroup is the base.
    const bounds = guard.taskCpuBounds(.empty, setOf(&.{4}), setOf(&.{ 4, 5 }));
    const scoped = guard.scopeCpuinfoWithBase(a, joined, bounds.allowed.?, bounds.base_cpus).?;
    try std.testing.expect(!contains(scoped, "waitpkg"));

    const base = try scratch(a, "scope-probe");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const path = try std.fmt.allocPrint(a, "{s}/cpuinfo", .{base});
    try writeFile(path, scoped);
    const flags = guard.readCpuFlags(a, path);
    const chain = [_][]const u8{ "x86_64/intel/sapphirerapids", "x86_64/intel/icelake", "x86_64/generic" };
    var chosen: []const u8 = "";
    for (chain) |candidate| if (guard.candidateSupportsCpu(candidate, flags)) {
        chosen = candidate;
        break;
    };
    try std.testing.expectEqualStrings("x86_64/intel/icelake", chosen);
}

test "a cgroup v2 cpuset narrows a wider sched affinity before the cpuinfo is scoped" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const emr_text = try readFixture(a, "emeraldrapids-kvm-no-waitpkg.cpuinfo");
    const base = try scratch(a, "cgv2");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};

    const cg_root = try std.fmt.allocPrint(a, "{s}/sys_fs_cgroup", .{base});
    const step = try std.fmt.allocPrint(a, "{s}/system.slice/slurmstepd.scope/job_42/step_0", .{cg_root});
    try writeUnder(try std.fmt.allocPrint(a, "{s}/cpuset.cpus.effective", .{step}), "2-3\n");
    try writeUnder(try std.fmt.allocPrint(a, "{s}/task_0/cpuset.cpus.effective", .{step}), "\n");
    const proc_cg = try std.fmt.allocPrint(a, "{s}/proc_cgroup", .{base});
    try writeFile(proc_cg, "0::/system.slice/slurmstepd.scope/job_42/step_0/task_0\n");
    const cg_set = guard.cgroupCpusFrom(a, proc_cg, cg_root).?;
    try std.testing.expect(cg_set.eql(setOf(&.{ 2, 3 })));

    const ns_root = try std.fmt.allocPrint(a, "{s}/ns_cgroup", .{base});
    try writeUnder(try std.fmt.allocPrint(a, "{s}/cpuset.cpus.effective", .{ns_root}), "4,6\n");
    const ns_proc = try std.fmt.allocPrint(a, "{s}/ns_proc_cgroup", .{base});
    try writeFile(ns_proc, "0::/\n");
    try std.testing.expect(guard.cgroupCpusFrom(a, ns_proc, ns_root).?.eql(setOf(&.{ 4, 6 })));

    const wide = guard.taskCpuBounds(.empty, setOf(&.{ 0, 1, 2, 3, 4, 5, 6, 7 }), cg_set);
    try std.testing.expect(wide.base_cpus.?.eql(setOf(&.{ 2, 3 })));
    try std.testing.expect(wide.allowed.?.eql(setOf(&.{ 2, 3 })));
    const narrow = guard.taskCpuBounds(.empty, setOf(&.{2}), cg_set);
    try std.testing.expect(narrow.base_cpus.?.eql(setOf(&.{ 2, 3 })));
    try std.testing.expect(narrow.allowed.?.eql(setOf(&.{2})));

    const scoped = guard.scopeCpuinfoWithBase(a, emr_text, wide.allowed.?, wide.base_cpus).?;
    try std.testing.expect(contains(scoped, "model\t\t: 207") or contains(scoped, "model : 207"));
    try std.testing.expect(contains(scoped, "amx_tile"));
    try std.testing.expect(!contains(scoped, "waitpkg"));
}

test "a cgroup path starting with two slashes does not adopt a cpuset from the working directory" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try scratch(a, "dslash");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};

    // cpuset.cpus.effective in the working directory names processor 1; the
    // cgroup path "//job" has the directory "/" and reads nothing relative.
    const stray = "cpuset.cpus.effective";
    try writeFile(stray, "1\n");
    defer {
        var zb: [sys.path_max:0]u8 = undefined;
        if (sys.z(&zb, stray)) |z| _ = linux.unlink(z.ptr) else |_| {}
    }
    const proc_cg = try std.fmt.allocPrint(a, "{s}/cgroup", .{base});
    const mountinfo = try std.fmt.allocPrint(a, "{s}/mountinfo", .{base});
    try writeFile(proc_cg, "0:://job\n");
    try writeFile(mountinfo, "22 20 0:22 / / rw - cgroup2 cgroup2 rw\n");
    const cg_root = try std.fmt.allocPrint(a, "{s}/sys/fs/cgroup", .{base});
    makeDirs(cg_root);
    const found = guard.cgroupCpusFrom(a, proc_cg, cg_root);

    const cpuinfo =
        \\processor : 0
        \\vendor_id : GenuineIntel
        \\cpu family : 6
        \\model : 60
        \\flags : avx2 fma
        \\
        \\processor : 1
        \\vendor_id : GenuineIntel
        \\cpu family : 6
        \\model : 143
        \\flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl avx512_bf16 amx_tile waitpkg
        \\
    ;
    const bounds = guard.taskCpuBounds(.empty, setOf(&.{ 0, 1 }), found);
    const scoped = guard.scopeCpuinfoWithBase(a, cpuinfo, bounds.allowed.?, bounds.base_cpus).?;
    try std.testing.expect(!contains(scoped, "waitpkg"));
    try std.testing.expect(contains(scoped, "avx2"));
    try std.testing.expect(contains(scoped, "fma"));
}

test "the waitpkg march rule needs the flag on every flags line" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const real_text = try readFixture(a, "sapphirerapids-8488C.cpuinfo");
    const kvm_text = try readFixture(a, "sapphirerapids-kvm-no-waitpkg.cpuinfo");
    const base = try scratch(a, "waitpkg");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const real_path = try std.fmt.allocPrint(a, "{s}/real", .{base});
    const kvm_path = try std.fmt.allocPrint(a, "{s}/kvm", .{base});
    const mixed_path = try std.fmt.allocPrint(a, "{s}/mixed", .{base});
    try writeFile(real_path, real_text);
    try writeFile(kvm_path, kvm_text);
    try writeFile(mixed_path, "flags : avx2 waitpkg fma\nflags : avx2 fma\n");

    const real_flags = guard.readCpuFlags(a, real_path);
    const kvm_flags = guard.readCpuFlags(a, kvm_path);
    try std.testing.expect(text.inList(real_flags, "waitpkg"));
    try std.testing.expect(text.inList(kvm_flags, "amx_tile"));
    try std.testing.expect(text.inList(kvm_flags, "avx512_bf16"));
    try std.testing.expect(!text.inList(kvm_flags, "waitpkg"));

    const spr = "x86_64/intel/sapphirerapids";
    try std.testing.expect(guard.candidateSupportsCpu(spr, real_flags));
    try std.testing.expect(!guard.candidateSupportsCpu(spr, kvm_flags));
    for (guard.waitpkg_marches) |march| {
        const path = try std.fmt.allocPrint(a, "x86_64/intel/{s}", .{march});
        try std.testing.expect(guard.candidateSupportsCpu(path, real_flags));
        try std.testing.expect(!guard.candidateSupportsCpu(path, kvm_flags));
        const hyphen = try a.dupe(u8, path);
        std.mem.replaceScalar(u8, hyphen, '_', '-');
        try std.testing.expect(!guard.candidateSupportsCpu(hyphen, &.{}));
    }
    try std.testing.expect(guard.candidateSupportsCpu("x86_64/amd/zen4", kvm_flags));
    try std.testing.expect(guard.candidateSupportsCpu("x86_64/generic", kvm_flags));

    const mixed = guard.readCpuFlags(a, mixed_path);
    try std.testing.expect(!text.inList(mixed, "waitpkg"));
    try std.testing.expect(text.inList(mixed, "fma"));
    try std.testing.expect(!guard.candidateSupportsCpu(spr, mixed));

    try std.testing.expectEqual(@as(usize, 0), guard.readCpuFlags(a, "/no/such/eessi-cpuinfo").len);
    try std.testing.expect(!guard.candidateSupportsCpu(spr, &.{}));
    // A directory is no cpuinfo: no flags, and no error.
    try std.testing.expectEqual(@as(usize, 0), guard.readCpuFlags(a, base).len);
}

fn bindEnv(a: Allocator, key: []const u8, value: []const u8) !envmap.Map {
    var env: envmap.Map = .empty;
    try env.put(a, key, value);
    return env;
}

fn expectIds(set: ?CpuSet, ids: []const usize) !void {
    const found = set orelse return error.TestExpectedEqual;
    for (0..guard.cpu_id_max) |cpu| {
        var want = false;
        for (ids) |id| if (id == cpu) {
            want = true;
        };
        try std.testing.expectEqual(want, found.has(cpu));
    }
}

test "a cpu bind spec is a mask, a list or a hostlist, in either Slurm variable" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    try expectIds(guard.parseCpuSpec("0x2"), &.{1});
    try expectIds(guard.parseCpuSpec("0-1,4"), &.{ 0, 1, 4 });
    try expectIds(guard.parseCpuSpec("cpu[0-1,4]"), &.{ 0, 1, 4 });
    try expectIds(guard.parseCpuSpec("[1]"), &.{1});
    try std.testing.expect(guard.parseCpuSpec("nid[00001-00002]") == null);
    try std.testing.expect(guard.parseCpuSpec("quiet") == null);
    try std.testing.expect(guard.parseCpuSpec("0 1") == null);
    try std.testing.expect(guard.parseCpuSpec("0 - 1") == null);
    try expectIds(guard.parseCpuSpec("0, 1"), &.{ 0, 1 });
    try expectIds(guard.parseCpuSpec("cpu[0, 1]"), &.{ 0, 1 });

    try expectIds(guard.bindCpus(try bindEnv(a, "SLURM_CPU_BIND_LIST", "cpu[1]")), &.{1});
    try expectIds(guard.bindCpus(try bindEnv(a, "SLURM_CPU_BIND", "quiet,mask_cpu:0x2")), &.{1});
    try expectIds(guard.bindCpus(try bindEnv(a, "SLURM_CPU_BIND_LIST", "0x2,0x4")), &.{ 1, 2 });
    try expectIds(guard.bindCpus(try bindEnv(a, "SLURM_CPU_BIND", "quiet,mask_cpu:0x2,0x4")), &.{ 1, 2 });
}

fn digestOf(a: Allocator, env: envmap.Map) []const u8 {
    var c: guard.Ctx = .{ .arena = a, .env = env, .strict = false, .self_exe = "" };
    return guard.cacheDigest(&c, env);
}

fn baseEnv(a: Allocator) !envmap.Map {
    var env: envmap.Map = .empty;
    try env.put(a, "EESSI_SOFTWARE_SUBDIR", "x86_64/amd/zen4");
    try env.put(a, "LOADEDMODULES", "EESSI/2025.06:R/4.4.2");
    try env.put(a, "MODULEPATH", "/mod");
    try env.put(a, "PATH", "/usr/bin:/bin");
    try env.put(a, "_ModuleTable001_", "old-table");
    return env;
}

test "the cache digest ignores the guard's own switches and follows the bind list and visible GPU" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    var left = try baseEnv(a);
    try left.put(a, "EESSI_ARCH_GUARD", "0");
    try left.put(a, "EESSI_ARCH_GUARD_MISSING", "ignore");
    var right = try baseEnv(a);
    try right.put(a, "EESSI_ARCH_GUARD", "1");
    try std.testing.expectEqualStrings(digestOf(a, left), digestOf(a, right));
    try right.put(a, "LOADEDMODULES", "EESSI/2025.06:Other/1.0");
    try std.testing.expect(!std.mem.eql(u8, digestOf(a, left), digestOf(a, right)));

    var bound = try baseEnv(a);
    try bound.put(a, "SLURM_CPU_BIND_LIST", "cpu[0]");
    var other = try baseEnv(a);
    try other.put(a, "SLURM_CPU_BIND_LIST", "cpu[1]");
    try std.testing.expect(!std.mem.eql(u8, digestOf(a, bound), digestOf(a, other)));

    var gpu = try baseEnv(a);
    try gpu.put(a, "CUDA_VISIBLE_DEVICES", "0");
    var gpu_other = try baseEnv(a);
    try gpu_other.put(a, "CUDA_VISIBLE_DEVICES", "1");
    try std.testing.expect(!std.mem.eql(u8, digestOf(a, gpu), digestOf(a, gpu_other)));
    try std.testing.expectEqualStrings(digestOf(a, gpu), digestOf(a, gpu));
}

test "the cache directory is TMPDIR when it exists and /tmp otherwise" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var c: guard.Ctx = .{ .arena = a, .env = .empty, .strict = false, .self_exe = "" };

    var env: envmap.Map = .empty;
    try env.put(a, "TMPDIR", "/no/such/eessi-arch-guard-dir");
    try std.testing.expectEqualStrings("/tmp", guard.cacheDirectory(&c, env).?);

    const base = try scratch(a, "cachedir");
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    try env.put(a, "TMPDIR", base);
    try std.testing.expectEqualStrings(base, guard.cacheDirectory(&c, env).?);
    var zb: [sys.path_max:0]u8 = undefined;
    _ = linux.rmdir((try sys.z(&zb, base)).ptr);
    try std.testing.expectEqualStrings("/tmp", guard.cacheDirectory(&c, env).?);
}
