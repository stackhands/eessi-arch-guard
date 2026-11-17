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

if failed then
    os.exit(1)
end
print("ok")
