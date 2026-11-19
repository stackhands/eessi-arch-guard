-- Warn when a module file under software/linux/SUBDIR/modules is loaded
-- and EESSI_SOFTWARE_SUBDIR names a different subdirectory.
-- An accelerator module lives at SUBDIR/accel/<vendor>/<target>/modules.
-- Those segments are not part of SUBDIR.
-- warn_accel_mismatch = 1, or EESSI_WARN_ACCEL_MISMATCH=1, also warns when
-- that accelerator differs from eessi_archdetect.sh accelpath. Off unless
-- a site asks. The load continues. Lmod does not run this hook for a job
-- that inherited its modules; the TaskProlog still reloads that stack.
-- Module usage tracking is separate. It stays off unless
-- EESSI_USAGE_TRACKING or usage_tracking asks. ExitHookA then sends one
-- ModuleUsageTracking record per load, for eb-keep-logs.

local M = {}

-- CPU subdirectory, and the accelerator subdirectory when the module
-- file is under one. The accelerator form matches the guard:
-- accel/nvidia/cc<digits> or accel/amd/gfx<hex>.
function M.path_parts(fn)
    if type(fn) ~= "string" then
        return nil, nil
    end
    local norm = fn:gsub("/+", "/")
    local best_body = nil
    local pos = 1
    while true do
        local _, e = norm:find("/software/linux/", pos, true)
        if e == nil then
            break
        end
        local candidate = norm:sub(e + 1):match("^(.-)/modules/")
        if candidate ~= nil and candidate ~= "" and not candidate:find("/software/linux/", 1, true) then
            best_body = candidate
        end
        pos = e + 1
    end
    if best_body == nil then
        return nil, nil
    end
    local cpu, accel = best_body:match("^(.-)/(accel/nvidia/cc%d+)$")
    if cpu == nil or cpu == "" then
        cpu, accel = best_body:match("^(.-)/(accel/amd/gfx[0-9a-f]+)$")
    end
    if cpu ~= nil and cpu ~= "" and accel ~= nil then
        return cpu, accel
    end
    return best_body, nil
end

function M.software_subdir(fn)
    local cpu = M.path_parts(fn)
    return cpu
end

function M.warn_mismatch(fn, expected, fullname)
    local got = M.software_subdir(fn)
    if got == nil or expected == nil or expected == "" or got == expected then
        return nil
    end
    local name = fullname
    if type(name) ~= "string" or name == "" then
        name = fn
    end
    return string.format(
        "eessi-arch-guard: %s is under software/linux/%s but EESSI_SOFTWARE_SUBDIR is %s\n",
        name,
        got,
        expected
    )
end

function M.module_accel(fn)
    local _, accel = M.path_parts(fn)
    return accel
end

-- nil unless text is one accelerator subdirectory, the form accelpath prints.
function M.accel_subdir(text)
    if type(text) ~= "string" then
        return nil
    end
    if text:match("^accel/nvidia/cc%d+$") or text:match("^accel/amd/gfx[0-9a-f]+$") then
        return text
    end
    return nil
end

function M.warn_accel(fn, node_accel, fullname)
    local got = M.module_accel(fn)
    local node = M.accel_subdir(node_accel)
    if got == nil or node == nil or got == node then
        return nil
    end
    local name = fullname
    if type(name) ~= "string" or name == "" then
        name = fn
    end
    local got_cc = got:match("^accel/nvidia/(cc%d+)$")
    local node_cc = node:match("^accel/nvidia/(cc%d+)$")
    local shown_got = got
    local shown_node = node
    if got_cc ~= nil and node_cc ~= nil then
        shown_got = got_cc
        shown_node = node_cc
    end
    return string.format(
        "eessi-arch-guard: %s is %s but this node is %s\n",
        name,
        shown_got,
        shown_node
    )
end

-- Toolchain short names hidden from module avail when a site turns the
-- list on. A user can still module load the full name. Nothing is hidden
-- while EESSI_HIDE_TOOLCHAINS and hide_toolchains are unset.
M.default_toolchains = {
    "GCC",
    "GCCcore",
    "foss",
    "gfbf",
    "gomkl",
    "gompi",
    "iimpi",
    "intel",
    "intel-compilers",
}

function M.normal_spec(spec)
    if type(spec) ~= "string" then
        return nil
    end
    local trimmed = spec:match("^%s*(.-)%s*$")
    if trimmed == nil or trimmed == "" then
        return nil
    end
    return trimmed
end

-- nil when the spec is unset. "1", "true" and "on" select the default
-- list. "0", "false" and "off" select none. Any other value is a comma
-- separated list: a short name hides every version, and NAME/VERSION
-- hides that one module.
function M.hide_rules(spec)
    local trimmed = M.normal_spec(spec)
    if trimmed == nil then
        return nil
    end
    if trimmed == "1" or trimmed == "true" or trimmed == "on" then
        return M.default_toolchains
    end
    if trimmed == "0" or trimmed == "false" or trimmed == "off" then
        return {}
    end
    local rules = {}
    for item in string.gmatch(trimmed, "[^,]+") do
        local name = item:match("^%s*(.-)%s*$")
        if name ~= "" then
            rules[#rules + 1] = name
        end
    end
    return rules
end

function M.toolchain_hidden(fullName, sn, spec)
    local rules = M.hide_rules(spec)
    if rules == nil then
        return false
    end
    if type(fullName) ~= "string" and type(sn) ~= "string" then
        return false
    end
    for _, rule in ipairs(rules) do
        if rule == fullName or rule == sn then
            return true
        end
    end
    return false
end

-- The same line grammar as the guard: strip, skip blanks and comments,
-- split at the first "=", last key wins. An empty value is unset.
function M.conf_value(text, key)
    if type(text) ~= "string" or type(key) ~= "string" then
        return nil
    end
    local found = nil
    for line in (text .. "\n"):gmatch("(.-)\r?\n") do
        local stripped = line:match("^%s*(.-)%s*$")
        if stripped ~= "" and stripped:sub(1, 1) ~= "#" then
            local eq = stripped:find("=", 1, true)
            if eq then
                local k = stripped:sub(1, eq - 1):match("^%s*(.-)%s*$")
                local v = stripped:sub(eq + 1):match("^%s*(.-)%s*$")
                if k == key then
                    found = v
                end
            end
        end
    end
    return found
end

-- The environment wins when it is set. Otherwise the conf key. An empty
-- environment value is unset, so the key applies.
function M.effective_key(env_value, conf_text, key)
    local from_env = M.normal_spec(env_value)
    if from_env ~= nil then
        return from_env
    end
    return M.normal_spec(M.conf_value(conf_text, key))
end

function M.effective_spec(env_value, conf_text)
    return M.effective_key(env_value, conf_text, "hide_toolchains")
end

-- 1, true and on ask for the accelerator check. 0, false and off do not.
-- Any other value leaves it off.
function M.warn_accel_on(env_value, conf_text)
    local spec = M.effective_key(env_value, conf_text, "warn_accel_mismatch")
    return spec == "1" or spec == "true" or spec == "on"
end

-- 1, true, on and yes ask for usage tracking, in any case. Anything else,
-- including unset, is off. This is not the accelerator check: that one
-- does not treat "yes" as on, and it does not ignore case.
function M.flag_enabled(spec)
    local trimmed = M.normal_spec(spec)
    if trimmed == nil then
        return false
    end
    local lower = trimmed:lower()
    return lower == "1" or lower == "true" or lower == "on" or lower == "yes"
end

-- No arguments reads EESSI_USAGE_TRACKING, then the site file. Passed
-- arguments are the env value and the conf text, including explicit nil.
function M.usage_tracking_enabled(...)
    local env_value, conf_text
    if select("#", ...) == 0 then
        env_value = os.getenv("EESSI_USAGE_TRACKING")
        local from_env = M.normal_spec(env_value)
        if from_env ~= nil then
            return M.flag_enabled(from_env)
        end
        conf_text = M.site_conf()
    else
        env_value, conf_text = ...
    end
    return M.flag_enabled(M.effective_key(env_value, conf_text, "usage_tracking"))
end

-- Absolute, and safe to pass to a shell: no space or metacharacter, no
-- empty, "." or ".." segment. EESSI_INIT_PREFIX is that kind of path.
function M.safe_init(path)
    if type(path) ~= "string" or path:sub(1, 1) ~= "/" then
        return false
    end
    if path:find("[^A-Za-z0-9._+/-]") then
        return false
    end
    for seg in path:gmatch("[^/]+") do
        if seg == "." or seg == ".." then
            return false
        end
    end
    return true
end

function M.archdetect_command(init)
    return "bash --noprofile --norc -- " .. init .. "/eessi_archdetect.sh accelpath"
end

function M.command_succeeded(ok)
    return ok == true or ok == 0
end

function M.read_command(cmd)
    local pipe = io.popen(cmd, "r")
    if pipe == nil then
        return nil
    end
    local text = pipe:read("*a")
    local ok = pipe:close()
    if not M.command_succeeded(ok) or type(text) ~= "string" then
        return nil
    end
    local line = text:match("^%s*(.-)%s*$")
    if line == nil or line == "" or line:find("[\n\r]") then
        return nil
    end
    return M.accel_subdir(line)
end

-- This node's accelerator, from the same script the guard runs.
-- A missing script, a failed script, or any other output is nil.
function M.accel_from_archdetect()
    local init = os.getenv("EESSI_INIT_PREFIX")
    if not M.safe_init(init) then
        return nil
    end
    local script = init .. "/eessi_archdetect.sh"
    local handle = io.open(script, "r")
    if handle == nil then
        return nil
    end
    handle:close()
    return M.read_command(M.archdetect_command(init))
end

function M.read_node_accel()
    if M.node_accel_cached then
        return M.node_accel_value
    end
    M.node_accel_cached = true
    M.node_accel_value = M.accel_from_archdetect()
    return M.node_accel_value
end

function M.package_dir()
    local src = debug.getinfo(1, "S").source
    if type(src) == "string" and src:sub(1, 1) == "@" then
        return src:sub(2):match("^(.*)/") or "."
    end
    return "."
end

function M.conf_path_list()
    if type(M.conf_paths) == "table" then
        return M.conf_paths
    end
    return {
        "/etc/eessi-arch-guard.conf",
        M.package_dir() .. "/eessi-arch-guard.conf",
    }
end

-- The first path that opens. This does not apply the guard's ownership
-- checks: Lmod reads the file as the user who ran module.
function M.site_conf()
    for _, path in ipairs(M.conf_path_list()) do
        local handle = io.open(path, "r")
        if handle then
            local text = handle:read("*a")
            handle:close()
            if type(text) == "string" then
                return text
            end
        end
    end
    return nil
end

-- Lmod's isVisibleHook passes fullName, sn, fn and isVisible. Setting
-- isVisible false hides the module from avail. It does not stop a load
-- of the full name, and it does not clear a module Lmod has already hidden.
function M.is_visible_hook(modT)
    if type(modT) ~= "table" then
        return
    end
    local spec = M.effective_spec(os.getenv("EESSI_HIDE_TOOLCHAINS"), M.site_conf())
    if M.toolchain_hidden(modT.fullName, modT.sn, spec) then
        modT.isVisible = false
    end
end

-- Records are buffered during load and flushed via ExitHookA in the
-- space-delimited key=value format eb-keep-logs parses. jobid is optional
-- there and is ignored when present.
M.usage_records = {}
M.exec_cmd = os.execute

function M.find_posix_syslog()
    local ok_s, mod_s = pcall(require, "posix.syslog")
    if ok_s and type(mod_s) == "table" and type(mod_s.syslog) == "function" then
        return mod_s
    end
    local ok_p, mod_p = pcall(require, "posix")
    if ok_p and type(mod_p) == "table" then
        if type(mod_p.syslog) == "table" and type(mod_p.syslog.syslog) == "function" then
            return mod_p.syslog
        end
        if type(mod_p.syslog) == "function" then
            return mod_p
        end
    end
    return nil
end

local function sanitize_field(value, fallback)
    if type(value) ~= "string" or not value:match("%S") then
        return fallback
    end
    local cleaned = value:gsub("[%s%c]+", "_")
    if cleaned == "" then
        return fallback
    end
    return cleaned
end

local function current_user()
    local user = os.getenv("USER")
    if type(user) ~= "string" or user == "" then
        user = os.getenv("LOGNAME")
    end
    return sanitize_field(user, "unknown")
end

local function current_host()
    local ok, posix = pcall(require, "posix")
    if ok and type(posix) == "table" and type(posix.uname) == "function" then
        local ok_u, host = pcall(posix.uname, "%n")
        if ok_u and type(host) == "string" and host ~= "" then
            return sanitize_field(host, "unknown")
        end
        if ok_u and type(host) == "table" and type(host.nodename) == "string" and host.nodename ~= "" then
            return sanitize_field(host.nodename, "unknown")
        end
    end
    local env_host = os.getenv("HOSTNAME")
    if type(env_host) == "string" and env_host ~= "" then
        return sanitize_field(env_host, "unknown")
    end
    local fh = io.open("/proc/sys/kernel/hostname", "r")
    if fh then
        local line = fh:read("*l")
        fh:close()
        if type(line) == "string" and line ~= "" then
            return sanitize_field(line, "unknown")
        end
    end
    return "unknown"
end

local function current_epoch()
    local epoch_fn = rawget(_G, "epoch")
    if type(epoch_fn) == "function" then
        local ok, value = pcall(epoch_fn)
        local num = ok and tonumber(value) or nil
        if num ~= nil and num >= 0 and num == num and num ~= math.huge then
            return num
        end
    end
    local ok, posix = pcall(require, "posix")
    if ok and type(posix) == "table" and type(posix.gettimeofday) == "function" then
        local ok_t, a, b = pcall(posix.gettimeofday)
        if ok_t then
            if type(a) == "table" and a.sec ~= nil then
                return a.sec + (a.usec or 0) * 1.0e-6
            elseif type(a) == "number" then
                return a + (type(b) == "number" and b or 0) * 1.0e-6
            end
        end
    end
    return os.time()
end

local function shell_quote(text)
    return "'" .. tostring(text):gsub("'", "'\\''") .. "'"
end

function M.format_usage_record(t)
    if type(t) ~= "table" then
        return nil
    end
    local mod_name = sanitize_field(t.modFullName, nil)
    local fn = sanitize_field(t.fn, nil)
    if mod_name == nil or fn == nil then
        return nil
    end
    local user = current_user()
    local jobid = sanitize_field(os.getenv("SLURM_JOB_ID"), "nil")
    local host = current_host()
    local ts = string.format("%.6f", current_epoch())
    return string.format(
        "user=%s jobid=%s module=%s path=%s host=%s time=%s",
        user,
        jobid,
        mod_name,
        fn,
        host,
        ts
    )
end

function M.record_load(t)
    if not M.usage_tracking_enabled() then
        return nil
    end
    local mode_fn = rawget(_G, "mode")
    if type(mode_fn) == "function" then
        local ok, cur = pcall(mode_fn)
        if not ok or cur ~= "load" then
            return nil
        end
    end
    local line = M.format_usage_record(t)
    if line ~= nil then
        M.usage_records[#M.usage_records + 1] = line
    end
    return line
end

function M.emit_syslog(records)
    if type(records) ~= "table" or #records == 0 then
        return true
    end
    local sys = M.find_posix_syslog()
    if sys ~= nil then
        if type(sys.openlog) == "function" then
            pcall(sys.openlog, "ModuleUsageTracking", sys.LOG_PID or 0, sys.LOG_LOCAL0 or 128)
        end
        local level = sys.LOG_INFO or 6
        for _, line in ipairs(records) do
            pcall(sys.syslog, level, line)
        end
        if type(sys.closelog) == "function" then
            pcall(sys.closelog)
        end
        return true
    end
    for _, line in ipairs(records) do
        local cmd = "logger -t ModuleUsageTracking -p local0.info -- " .. shell_quote(line) .. " >/dev/null 2>&1"
        pcall(M.exec_cmd, cmd)
    end
    return true
end

function M.flush_usage_records()
    local records = M.usage_records
    M.usage_records = {}
    if type(records) ~= "table" or #records == 0 then
        return 0
    end
    M.emit_syslog(records)
    return #records
end

function M.load_hook(t)
    local fn, fullname
    if type(t) == "table" then
        fn = t.fn
        fullname = t.modFullName
    end
    local msg = M.warn_mismatch(fn, os.getenv("EESSI_SOFTWARE_SUBDIR"), fullname)
    if msg then
        io.stderr:write(msg)
    end
    -- A CPU-only module does not read the node, and a disabled accelerator
    -- check does not either. Recording runs after both warnings.
    if M.module_accel(fn) ~= nil
        and M.warn_accel_on(os.getenv("EESSI_WARN_ACCEL_MISMATCH"), M.site_conf())
    then
        local accel_msg = M.warn_accel(fn, M.read_node_accel(), fullname)
        if accel_msg then
            io.stderr:write(accel_msg)
        end
    end
    M.record_load(t)
end

-- Lmod loads this file under tools/strict.lua. `hook` is a local in Lmod's
-- own files, not a declared global, so a bare read aborts the command.
-- The unit test installs a global; Lmod itself is require("Hook").
-- Registrations stay load and isVisibleHook, append on Lmod 8.7.35+,
-- plain register when an older Lmod rejects the third argument.
local function resolve_hook()
    local global_hook = rawget(_G, "hook")
    if type(global_hook) == "table" and type(global_hook.register) == "function" then
        return global_hook
    end
    local ok, loaded = pcall(require, "Hook")
    if ok and type(loaded) == "table" and type(loaded.register) == "function" then
        return loaded
    end
    return nil
end

local hook_tbl = resolve_hook()
if hook_tbl ~= nil then
    local function register(name, fn)
        if not pcall(hook_tbl.register, name, fn, "append") then
            hook_tbl.register(name, fn)
        end
    end
    register("load", M.load_hook)
    register("isVisibleHook", M.is_visible_hook)
end

-- Older Lmod has no ExitHookA. rawget avoids a missing-global error.
local exit_hook = rawget(_G, "ExitHookA")
if type(exit_hook) == "table" and type(exit_hook.register) == "function" then
    pcall(exit_hook.register, M.flush_usage_records)
end

return M
