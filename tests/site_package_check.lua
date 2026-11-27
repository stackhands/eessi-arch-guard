-- Drive share/lmod/SitePackage.lua without Lmod. The hook global is absent here.
local root = arg[1]
local sp = dofile(root .. "/share/lmod/SitePackage.lua")

local failed = false
local function check(label, got, want)
    if got ~= want then
        io.stderr:write(label .. "\n got " .. tostring(got) .. "\n want " .. tostring(want) .. "\n")
        failed = true
    end
end

local zen3 = "/cvmfs/software.eessi.io/versions/2025.06/software/linux/x86_64/amd/zen3/modules/all/Example/1.0.lua"
check("subdir", sp.software_subdir(zen3), "x86_64/amd/zen3")
check("same target", sp.warn_mismatch(zen3, "x86_64/amd/zen3", "Example/1.0"), nil)
check("not an EESSI module file", sp.warn_mismatch("/opt/site/modules/Example/1.0.lua", "x86_64/amd/zen4", "Example/1.0"), nil)
check("no recorded subdirectory", sp.warn_mismatch(zen3, nil, "Example/1.0"), nil)
check("empty recorded subdirectory", sp.warn_mismatch(zen3, "", "Example/1.0"), nil)

local msg = sp.warn_mismatch(zen3, "x86_64/amd/zen4", "Example/1.0")
if type(msg) ~= "string" or not msg:find("software/linux/x86_64/amd/zen3", 1, true) or not msg:find("EESSI_SOFTWARE_SUBDIR is x86_64/amd/zen4", 1, true) then
    io.stderr:write("mismatch warning\n got " .. tostring(msg) .. "\n")
    failed = true
end

-- An accelerator tree is software/linux/<cpu>/accel/<vendor>/<target>/modules/.
-- The accelerator segments are not another CPU subdirectory.
local cc90 = "/cvmfs/software.eessi.io/versions/2025.06/software/linux/x86_64/amd/zen4/accel/nvidia/cc90/modules/all/CUDA/12.6.0.lua"
local gfx = "/cvmfs/software.eessi.io/versions/2025.06/software/linux/x86_64/amd/zen4/accel/amd/gfx90a/modules/all/Example/1.0.lua"
check("accel path cpu subdir", sp.software_subdir(cc90), "x86_64/amd/zen4")
check("amd accel path cpu subdir", sp.software_subdir(gfx), "x86_64/amd/zen4")
check("same cpu under an accel path", sp.warn_mismatch(cc90, "x86_64/amd/zen4", "CUDA/12.6.0"), nil)
check("same cpu under an amd accel path", sp.warn_mismatch(gfx, "x86_64/amd/zen4", "Example/1.0"), nil)
local accel_cpu = sp.warn_mismatch(cc90, "x86_64/amd/zen3", "CUDA/12.6.0")
if type(accel_cpu) ~= "string"
    or not accel_cpu:find("software/linux/x86_64/amd/zen4", 1, true)
    or accel_cpu:find("accel/nvidia/cc90", 1, true)
    or not accel_cpu:find("EESSI_SOFTWARE_SUBDIR is x86_64/amd/zen3", 1, true)
then
    io.stderr:write("accel path cpu mismatch\n got " .. tostring(accel_cpu) .. "\n")
    failed = true
end

local saved = os.getenv
os.getenv = function(name)
    if name == "EESSI_SOFTWARE_SUBDIR" then
        return "x86_64/amd/zen4"
    end
    return saved(name)
end
local chunks = {}
local real_err = io.stderr
io.stderr = {
    write = function(_, text)
        chunks[#chunks + 1] = text
        return real_err:write(text)
    end,
}
sp.load_hook({ fn = zen3, modFullName = "Example/1.0" })
sp.load_hook({ fn = zen3:gsub("zen3", "zen4"), modFullName = "Example/1.0" })
sp.load_hook({ fn = "/opt/site/modules/Other/2.0.lua", modFullName = "Other/2.0" })
io.stderr = real_err
os.getenv = saved
local written = table.concat(chunks)
if not written:find("Example/1.0 is under software/linux/x86_64/amd/zen3", 1, true) then
    io.stderr:write("load hook wrote\n" .. written .. "\n")
    failed = true
end
if written:find("zen4/modules", 1, true) or written:find("Other/2.0", 1, true) then
    io.stderr:write("load hook warned on a match\n" .. written .. "\n")
    failed = true
end

os.getenv = function(name)
    if name == "EESSI_SOFTWARE_SUBDIR" then
        return "x86_64/amd/zen4"
    end
    return saved(name)
end
chunks = {}
io.stderr = {
    write = function(_, text)
        chunks[#chunks + 1] = text
        return real_err:write(text)
    end,
}
sp.load_hook({ fn = cc90, modFullName = "CUDA/12.6.0" })
sp.load_hook({ fn = gfx, modFullName = "Example/1.0" })
io.stderr = real_err
os.getenv = saved
written = table.concat(chunks)
if written:find("CUDA/12.6.0", 1, true) or written:find("accel/", 1, true) then
    io.stderr:write("load hook warned on an accelerator path with the same cpu\n" .. written .. "\n")
    failed = true
end

check("unset hides nothing", sp.toolchain_hidden("GCC/13.2.0", "GCC", nil), false)
check("empty hides nothing", sp.toolchain_hidden("GCC/13.2.0", "GCC", ""), false)
check("one turns the default list on", sp.toolchain_hidden("GCC/13.2.0", "GCC", "1"), true)
check("one leaves an application", sp.toolchain_hidden("GROMACS/2024.1", "GROMACS", "1"), false)
check("intel-compilers is its own name", sp.toolchain_hidden("intel-compilers/2023.2.0", "intel-compilers", "1"), true)
check("zero shows every toolchain", sp.toolchain_hidden("GCC/13.2.0", "GCC", "0"), false)
check("false shows every toolchain", sp.toolchain_hidden("foss/2023b", "foss", "false"), false)
check("a named version hides that version", sp.toolchain_hidden("foss/2022b", "foss", "foss/2022b"), true)
check("a named version leaves the other versions", sp.toolchain_hidden("foss/2023b", "foss", "foss/2022b"), false)
check("a list can name a short name", sp.toolchain_hidden("GCC/12.3.0", "GCC", " foss , GCC "), true)

local conf_on = "chain = /bin/true\nhide_toolchains = 1\n# hide_toolchains = 0\n"
check("the conf key turns the list on", sp.toolchain_hidden("GCC/13.2.0", "GCC", sp.effective_spec(nil, conf_on)), true)
check("an empty env falls through to the conf", sp.toolchain_hidden("GCC/13.2.0", "GCC", sp.effective_spec("", conf_on)), true)
check("the env off wins over the conf", sp.toolchain_hidden("GCC/13.2.0", "GCC", sp.effective_spec("0", conf_on)), false)
check("a custom env hides only what it names", sp.toolchain_hidden("foss/2023b", "foss", sp.effective_spec("GCC", conf_on)), false)
check("no key hides nothing", sp.toolchain_hidden("GCC/13.2.0", "GCC", sp.effective_spec(nil, "timeout = 60\n")), false)
check("the last key wins", sp.toolchain_hidden("GCC/13.2.0", "GCC", sp.effective_spec(nil, "hide_toolchains = 1\nhide_toolchains = 0\n")), false)
check("a carriage return keeps the last value", sp.conf_value("hide_toolchains = 0\rhide_toolchains = 1\r", "hide_toolchains"), "1")
check("a carriage return ends a comment", sp.conf_value("# c\rhide_toolchains = 1\r", "hide_toolchains"), "1")
check("a trailing carriage return is still one value", sp.conf_value("hide_toolchains = 1\r", "hide_toolchains"), "1")

sp.conf_paths = { "/no/such/eessi-arch-guard.conf" }
os.getenv = function(name)
    if name == "EESSI_HIDE_TOOLCHAINS" or name == "EESSI_SOFTWARE_SUBDIR" then
        return nil
    end
    return saved(name)
end
local shown = { fullName = "GCC/13.2.0", sn = "GCC", fn = "/m/GCC/13.2.0.lua", isVisible = true }
local already = { fullName = "Hidden/1.0", sn = "Hidden", fn = "/m/Hidden/1.0.lua", isVisible = false }
sp.is_visible_hook(shown)
sp.is_visible_hook(already)
check("the hook hides nothing by default", shown.isVisible, true)
check("the hook leaves a module lmod already hid", already.isVisible, false)

local conf_file = os.tmpname()
local handle = io.open(conf_file, "w")
handle:write("hide_toolchains = 1\n")
handle:close()
sp.conf_paths = { conf_file }
local from_conf = { fullName = "GCC/13.2.0", sn = "GCC", fn = "/m/GCC/13.2.0.lua", isVisible = true }
local app = { fullName = "GROMACS/2024.1", sn = "GROMACS", fn = "/m/GROMACS/2024.1.lua", isVisible = true }
sp.is_visible_hook(from_conf)
sp.is_visible_hook(app)
os.getenv = function(name)
    if name == "EESSI_HIDE_TOOLCHAINS" then
        return "0"
    end
    if name == "EESSI_SOFTWARE_SUBDIR" then
        return nil
    end
    return saved(name)
end
local forced_on = { fullName = "GCC/13.2.0", sn = "GCC", fn = "/m/GCC/13.2.0.lua", isVisible = true }
sp.is_visible_hook(forced_on)
os.getenv = saved
os.remove(conf_file)
check("the hook reads hide_toolchains=1", from_conf.isVisible, false)
check("the hook leaves an application", app.isVisible, true)
check("the env off wins over the file", forced_on.isVisible, true)

check("module accel cc90", sp.module_accel(cc90), "accel/nvidia/cc90")
check("module accel gfx", sp.module_accel(gfx), "accel/amd/gfx90a")
check("cpu module has no accel", sp.module_accel(zen3), nil)
check("same cc does not warn", sp.warn_accel(cc90, "accel/nvidia/cc90", "CUDA/12.6.0"), nil)
check("same amd accel does not warn", sp.warn_accel(gfx, "accel/amd/gfx90a", "Example/1.0"), nil)
check("no node accel does not warn", sp.warn_accel(cc90, nil, "CUDA/12.6.0"), nil)
check("empty node accel does not warn", sp.warn_accel(cc90, "", "CUDA/12.6.0"), nil)
check("unrecognized node accel does not warn", sp.warn_accel(cc90, "cc80", "CUDA/12.6.0"), nil)
check("cpu module ignores the node accel", sp.warn_accel(zen3, "accel/nvidia/cc80", "Example/1.0"), nil)

local cc_msg = sp.warn_accel(cc90, "accel/nvidia/cc80", "CUDA/12.6.0")
if type(cc_msg) ~= "string" or not cc_msg:find("CUDA/12.6.0 is cc90 but this node is cc80", 1, true) then
    io.stderr:write("cc mismatch\n got " .. tostring(cc_msg) .. "\n")
    failed = true
end
local cross = sp.warn_accel(gfx, "accel/nvidia/cc80", "Example/1.0")
if type(cross) ~= "string" or not cross:find("Example/1.0 is accel/amd/gfx90a but this node is accel/nvidia/cc80", 1, true) then
    io.stderr:write("cross accel mismatch\n got " .. tostring(cross) .. "\n")
    failed = true
end
local amd_msg = sp.warn_accel(gfx, "accel/amd/gfx942", "Example/1.0")
if type(amd_msg) ~= "string" or not amd_msg:find("Example/1.0 is accel/amd/gfx90a but this node is accel/amd/gfx942", 1, true) then
    io.stderr:write("amd accel mismatch\n got " .. tostring(amd_msg) .. "\n")
    failed = true
end

check("flag unset is off", sp.warn_accel_on(nil, nil), false)
check("flag one is on", sp.warn_accel_on("1", nil), true)
check("flag true is on", sp.warn_accel_on("true", "warn_accel_mismatch = 0\n"), true)
check("flag on is on", sp.warn_accel_on("on", nil), true)
check("flag zero is off", sp.warn_accel_on("0", "warn_accel_mismatch = 1\n"), false)
check("flag false is off", sp.warn_accel_on("false", nil), false)
check("flag off is off", sp.warn_accel_on("off", nil), false)
check("empty env uses the conf", sp.warn_accel_on("", "warn_accel_mismatch = 1\n"), true)
check("empty env and no key is off", sp.warn_accel_on("", "timeout = 60\n"), false)
check("the last accel key wins", sp.warn_accel_on(nil, "warn_accel_mismatch = 1\nwarn_accel_mismatch = 0\n"), false)
check("a comment is not the accel key", sp.warn_accel_on(nil, "# warn_accel_mismatch = 1\n"), false)
check("other flag text is off", sp.warn_accel_on("yes", nil), false)

local real_read = sp.read_node_accel
local probes = 0
sp.read_node_accel = function()
    probes = probes + 1
    return "accel/nvidia/cc80"
end
sp.conf_paths = { "/no/such/eessi-arch-guard.conf" }
os.getenv = function(name)
    if name == "EESSI_SOFTWARE_SUBDIR" then
        return "x86_64/amd/zen4"
    end
    if name == "EESSI_WARN_ACCEL_MISMATCH" or name == "EESSI_HIDE_TOOLCHAINS" then
        return nil
    end
    return saved(name)
end
chunks = {}
io.stderr = {
    write = function(_, text)
        chunks[#chunks + 1] = text
        return real_err:write(text)
    end,
}
sp.load_hook({ fn = cc90, modFullName = "CUDA/12.6.0" })
io.stderr = real_err
written = table.concat(chunks)
check("the accel check stays off", written, "")
check("an off check does not read the node", probes, 0)

os.getenv = function(name)
    if name == "EESSI_SOFTWARE_SUBDIR" then
        return "x86_64/amd/zen4"
    end
    if name == "EESSI_WARN_ACCEL_MISMATCH" then
        return "1"
    end
    return saved(name)
end
chunks = {}
io.stderr = {
    write = function(_, text)
        chunks[#chunks + 1] = text
        return real_err:write(text)
    end,
}
sp.load_hook({ fn = cc90, modFullName = "CUDA/12.6.0" })
sp.load_hook({ fn = zen3:gsub("zen3", "zen4"), modFullName = "Example/1.0" })
io.stderr = real_err
written = table.concat(chunks)
if not written:find("CUDA/12.6.0 is cc90 but this node is cc80", 1, true) or written:find("Example/1.0", 1, true) then
    io.stderr:write("load hook accel warning\n" .. written .. "\n")
    failed = true
end
check("the load hook reads the node once per accel module", probes, 1)

local accel_conf = os.tmpname()
local accel_handle = io.open(accel_conf, "w")
accel_handle:write("warn_accel_mismatch = 1\n")
accel_handle:close()
sp.conf_paths = { accel_conf }
os.getenv = function(name)
    if name == "EESSI_SOFTWARE_SUBDIR" then
        return "x86_64/amd/zen3"
    end
    if name == "EESSI_WARN_ACCEL_MISMATCH" then
        return nil
    end
    return saved(name)
end
chunks = {}
io.stderr = {
    write = function(_, text)
        chunks[#chunks + 1] = text
        return real_err:write(text)
    end,
}
sp.load_hook({ fn = cc90, modFullName = "CUDA/12.6.0" })
io.stderr = real_err
written = table.concat(chunks)
if not written:find("software/linux/x86_64/amd/zen4", 1, true) or not written:find("is cc90 but this node is cc80", 1, true) then
    io.stderr:write("conf accel warning\n" .. written .. "\n")
    failed = true
end
os.getenv = function(name)
    if name == "EESSI_WARN_ACCEL_MISMATCH" then
        return "0"
    end
    if name == "EESSI_SOFTWARE_SUBDIR" then
        return "x86_64/amd/zen4"
    end
    return saved(name)
end
probes = 0
chunks = {}
io.stderr = {
    write = function(_, text)
        chunks[#chunks + 1] = text
        return real_err:write(text)
    end,
}
sp.load_hook({ fn = cc90, modFullName = "CUDA/12.6.0" })
io.stderr = real_err
os.remove(accel_conf)
os.getenv = saved
sp.read_node_accel = real_read
written = table.concat(chunks)
check("the env off wins over warn_accel_mismatch", written, "")
check("the env off does not read the node", probes, 0)

check("a hyphenated init prefix is safe", sp.safe_init("/opt/site/eessi-init"), true)
check("a dotdot init prefix is unsafe", sp.safe_init("/tmp/../etc"), false)

local marker = os.tmpname()
os.remove(marker)
os.getenv = function(name)
    if name == "EESSI_INIT_PREFIX" then
        return "/tmp/bad;touch " .. marker
    end
    return saved(name)
end
check("an unsafe init prefix is not a node accel", sp.accel_from_archdetect(), nil)
local marker_handle = io.open(marker, "r")
check("an unsafe init prefix does not run", marker_handle, nil)
if marker_handle then
    marker_handle:close()
end
os.remove(marker)
os.getenv = saved

local function write_archdetect(dir, body)
    local path = dir .. "/eessi_archdetect.sh"
    local handle = io.open(path, "w")
    handle:write(body)
    handle:close()
    return path
end

local init_dir = os.tmpname()
os.remove(init_dir)
if os.execute("mkdir -m 700 " .. init_dir) ~= true then
    io.stderr:write("could not create " .. init_dir .. "\n")
    failed = true
else
    local count = init_dir .. "/count"
    write_archdetect(init_dir, "#!/bin/sh\nprintf x >> '" .. count .. "'\nprintf 'accel/nvidia/cc80\\n'\n")
    os.getenv = function(name)
        if name == "EESSI_INIT_PREFIX" then
            return init_dir
        end
        return saved(name)
    end
    check("accelpath cc80", sp.accel_from_archdetect(), "accel/nvidia/cc80")
    sp.node_accel_cached = nil
    check("the node accel is cached", sp.read_node_accel(), "accel/nvidia/cc80")
    check("a second read uses the cache", sp.read_node_accel(), "accel/nvidia/cc80")
    local count_handle = io.open(count, "r")
    local count_text = count_handle and count_handle:read("*a") or ""
    if count_handle then
        count_handle:close()
    end
    check("accelpath runs once for the cached read", count_text, "xx")
    write_archdetect(init_dir, "#!/bin/sh\nprintf 'nope\\n'\nexit 0\n")
    sp.node_accel_cached = nil
    check("accelpath text that is not an accelerator", sp.accel_from_archdetect(), nil)
    write_archdetect(init_dir, "#!/bin/sh\nprintf 'accel/nvidia/cc80\\n'\nexit 3\n")
    check("a failed accelpath is no node accel", sp.accel_from_archdetect(), nil)
    os.getenv = saved
    os.execute("rm -rf " .. init_dir)
end

-- Usage tracking stays off until EESSI_USAGE_TRACKING or usage_tracking asks.
check("usage tracking defaults off", sp.usage_tracking_enabled(nil, nil), false)
check("usage tracking empty off", sp.usage_tracking_enabled("", ""), false)
check("usage tracking 0 off", sp.usage_tracking_enabled("0", "usage_tracking = 1\n"), false)
check("usage tracking false off", sp.usage_tracking_enabled("false", nil), false)
check("usage tracking off off", sp.usage_tracking_enabled("off", nil), false)
check("usage tracking 1 on", sp.usage_tracking_enabled("1", nil), true)
check("usage tracking true on", sp.usage_tracking_enabled("true", nil), true)
check("usage tracking ON on", sp.usage_tracking_enabled("ON", nil), true)
check("usage tracking yes on", sp.usage_tracking_enabled("yes", nil), true)
check("usage tracking conf 1 on", sp.usage_tracking_enabled(nil, "usage_tracking = 1\n# usage_tracking = 0\n"), true)
check("usage tracking empty env falls back to conf", sp.usage_tracking_enabled("", "usage_tracking = 1\n"), true)
check("usage tracking last key wins", sp.usage_tracking_enabled(nil, "usage_tracking = 1\nusage_tracking = 0\n"), false)
-- "yes" turns tracking on. The accelerator check still treats it as off.
check("yes does not enable the accelerator check", sp.warn_accel_on("yes", nil), false)

sp.conf_paths = { "/no/such/eessi-arch-guard.conf" }
sp.usage_records = {}
local called_cmds = {}
local saved_exec = sp.exec_cmd
local saved_finder = sp.find_posix_syslog
sp.exec_cmd = function(cmd)
    called_cmds[#called_cmds + 1] = cmd
    return true
end
sp.find_posix_syslog = function()
    return nil
end
os.getenv = function(name)
    if name == "EESSI_USAGE_TRACKING" or name == "EESSI_SOFTWARE_SUBDIR" or name == "SLURM_JOB_ID" or name == "EESSI_WARN_ACCEL_MISMATCH" then
        return nil
    end
    return saved(name)
end
sp.load_hook({ fn = zen3, modFullName = "Example/1.0" })
check("disabled records nothing", #sp.usage_records, 0)
check("disabled flush returns 0", sp.flush_usage_records(), 0)
check("disabled flush runs no command", #called_cmds, 0)

local cur_mode = "load"
local saved_mode = rawget(_G, "mode")
local saved_epoch = rawget(_G, "epoch")
_G.mode = function()
    return cur_mode
end
_G.epoch = function()
    return 1731840001.25
end

local env_map = {
    EESSI_USAGE_TRACKING = "1",
    USER = "alice",
    HOSTNAME = "node01",
    SLURM_JOB_ID = nil,
    EESSI_SOFTWARE_SUBDIR = nil,
    EESSI_WARN_ACCEL_MISMATCH = nil,
}
os.getenv = function(name)
    if env_map[name] ~= nil or name == "EESSI_USAGE_TRACKING" or name == "USER" or name == "HOSTNAME" or name == "SLURM_JOB_ID" or name == "EESSI_SOFTWARE_SUBDIR" or name == "EESSI_WARN_ACCEL_MISMATCH" then
        return env_map[name]
    end
    return saved(name)
end

cur_mode = "unload"
sp.load_hook({ fn = zen3, modFullName = "Example/1.0" })
check("unload mode skips recording", #sp.usage_records, 0)

cur_mode = "load"
sp.load_hook({ fn = "", modFullName = "Example/1.0" })
sp.load_hook({ fn = zen3, modFullName = "" })
check("empty path or module skips recording", #sp.usage_records, 0)

sp.load_hook({ fn = zen3, modFullName = "GROMACS/2024.1" })
env_map.SLURM_JOB_ID = "4231"
sp.load_hook({ fn = "/opt/site/modules/Other'Mod/2.0.lua", modFullName = "Other'Mod/2.0" })
check("two loads buffered", #sp.usage_records, 2)

local rec1 = sp.usage_records[1]
local rec2 = sp.usage_records[2]
local function parse_kv(line)
    local kv = {}
    local count = 0
    for token in string.gmatch(line, "%S+") do
        local k, v = token:match("^([^=]+)=(.+)$")
        if k and v then
            kv[k] = v
            count = count + 1
        end
    end
    return kv, count
end

local kv1, c1 = parse_kv(rec1)
check("rec1 has 6 fields", c1, 6)
check("rec1 user", kv1.user, "alice")
check("rec1 jobid nil when unset", kv1.jobid, "nil")
check("rec1 module", kv1.module, "GROMACS/2024.1")
check("rec1 path", kv1.path, zen3)
check("rec1 time", kv1.time, "1731840001.250000")
if type(kv1.host) ~= "string" or kv1.host == "" then
    io.stderr:write("rec1 host missing\n")
    failed = true
end

local kv2, c2 = parse_kv(rec2)
check("rec2 has 6 fields", c2, 6)
check("rec2 jobid", kv2.jobid, "4231")
check("rec2 module", kv2.module, "Other'Mod/2.0")

check("logger flush count", sp.flush_usage_records(), 2)
check("buffer cleared after flush", #sp.usage_records, 0)
check("second flush is empty", sp.flush_usage_records(), 0)
check("logger called twice", #called_cmds, 2)
if not called_cmds[1]:find("^logger %-t ModuleUsageTracking %-p local0%.info %-%- ") then
    io.stderr:write("unexpected logger command: " .. tostring(called_cmds[1]) .. "\n")
    failed = true
end
if not called_cmds[2]:find("Other'\\''Mod/2.0", 1, true) then
    io.stderr:write("logger single quote not escaped: " .. tostring(called_cmds[2]) .. "\n")
    failed = true
end

local syslog_calls = { open = 0, msgs = {}, close = 0 }
sp.find_posix_syslog = function()
    return {
        LOG_PID = 1,
        LOG_LOCAL0 = 128,
        LOG_INFO = 6,
        openlog = function(ident, opt, fac)
            syslog_calls.open = syslog_calls.open + 1
            syslog_calls.ident = ident
            syslog_calls.opt = opt
            syslog_calls.fac = fac
        end,
        syslog = function(level, msg)
            syslog_calls.msgs[#syslog_calls.msgs + 1] = { level = level, msg = msg }
        end,
        closelog = function()
            syslog_calls.close = syslog_calls.close + 1
        end,
    }
end
called_cmds = {}
sp.load_hook({ fn = zen3, modFullName = "GROMACS/2024.1" })
check("posix.syslog flush count", sp.flush_usage_records(), 1)
check("posix.syslog openlog called", syslog_calls.open, 1)
check("posix.syslog ident", syslog_calls.ident, "ModuleUsageTracking")
check("posix.syslog closelog called", syslog_calls.close, 1)
check("posix.syslog msg count", #syslog_calls.msgs, 1)
check("posix.syslog msg level", syslog_calls.msgs[1].level, 6)
check("posix.syslog msg body", syslog_calls.msgs[1].msg:find("module=GROMACS/2024.1", 1, true) ~= nil, true)
check("posix.syslog skips logger fallback", #called_cmds, 0)

-- An accelerator module still warns, and the same load is recorded.
local real_read = sp.read_node_accel
local accel_probes = 0
sp.read_node_accel = function()
    accel_probes = accel_probes + 1
    return "accel/nvidia/cc80"
end
env_map.EESSI_SOFTWARE_SUBDIR = "x86_64/amd/zen4"
env_map.EESSI_WARN_ACCEL_MISMATCH = "1"
local accel_chunks = {}
local real_err = io.stderr
io.stderr = {
    write = function(_, text)
        accel_chunks[#accel_chunks + 1] = text
        return real_err:write(text)
    end,
}
sp.load_hook({ fn = cc90, modFullName = "CUDA/12.6.0" })
io.stderr = real_err
local accel_written = table.concat(accel_chunks)
if not accel_written:find("CUDA/12.6.0 is cc90 but this node is cc80", 1, true) then
    io.stderr:write("tracking left the accelerator warning off\n" .. accel_written .. "\n")
    failed = true
end
check("tracking still reads the node for an accelerator module", accel_probes, 1)
check("an accelerator load is recorded", #sp.usage_records, 1)
if sp.usage_records[1] == nil or not sp.usage_records[1]:find("module=CUDA/12.6.0", 1, true) then
    io.stderr:write("accelerator load record\n" .. tostring(sp.usage_records[1]) .. "\n")
    failed = true
end
sp.usage_records = {}
sp.read_node_accel = real_read
env_map.EESSI_WARN_ACCEL_MISMATCH = nil
env_map.EESSI_SOFTWARE_SUBDIR = nil

local usage_conf = os.tmpname()
local ufh = io.open(usage_conf, "w")
ufh:write("usage_tracking = 1\n")
ufh:close()
sp.conf_paths = { usage_conf }
env_map.EESSI_USAGE_TRACKING = nil
sp.load_hook({ fn = zen3, modFullName = "FromConf/1.0" })
os.remove(usage_conf)
check("conf enables usage tracking", #sp.usage_records, 1)
sp.usage_records = {}

local dir_conf = os.tmpname()
os.remove(dir_conf)
if os.execute("mkdir -m 700 " .. dir_conf) ~= true then
    io.stderr:write("could not create " .. dir_conf .. "\n")
    failed = true
else
    local fallback_conf = os.tmpname()
    local ffh = io.open(fallback_conf, "w")
    ffh:write("hide_toolchains = 1\n")
    ffh:close()
    sp.conf_paths = { dir_conf, fallback_conf }
    check("site_conf skips directory candidate", sp.site_conf(), "hide_toolchains = 1\n")
    sp.conf_paths = { "/no/such/eessi-arch-guard.conf" }
    os.remove(fallback_conf)
    os.execute("rmdir " .. dir_conf)
end

local registered_hooks = {}
local registered_exit = {}
local saved_hook = rawget(_G, "hook")
local saved_exit_hook = rawget(_G, "ExitHookA")
_G.hook = {
    register = function(name, fn)
        registered_hooks[name] = fn
    end,
}
_G.ExitHookA = {
    register = function(fn)
        registered_exit[#registered_exit + 1] = fn
    end,
}
local sp2 = dofile(root .. "/share/lmod/SitePackage.lua")
_G.hook = saved_hook
_G.ExitHookA = saved_exit_hook
_G.mode = saved_mode
_G.epoch = saved_epoch
sp.exec_cmd = saved_exec
sp.find_posix_syslog = saved_finder
os.getenv = saved
check("load hook registered", registered_hooks["load"], sp2.load_hook)
check("isVisibleHook registered", registered_hooks["isVisibleHook"], sp2.is_visible_hook)
check("ExitHookA registered", #registered_exit, 1)
check("ExitHookA flushes records", registered_exit[1], sp2.flush_usage_records)

if failed then
    os.exit(1)
end
print("ok")
