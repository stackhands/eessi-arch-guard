-- Warn when a module file under software/linux/SUBDIR/modules is loaded
-- and EESSI_SOFTWARE_SUBDIR names a different subdirectory.
-- An accelerator module lives at SUBDIR/accel/<vendor>/<target>/modules.
-- Those segments are not part of SUBDIR.
-- The load continues. Lmod does not run this hook for a job that inherited
-- its modules; the TaskProlog still reloads that stack.

local M = {}

-- CPU subdirectory, and the accelerator subdirectory when the module
-- file is under one. The accelerator form matches the guard:
-- accel/nvidia/cc<digits> or accel/amd/gfx<hex>.
function M.path_parts(fn)
    if type(fn) ~= "string" then
        return nil, nil
    end
    local body = fn:match("/software/linux/(.-)/modules/")
    if body == nil then
        return nil, nil
    end
    local cpu, accel = body:match("^(.-)/(accel/nvidia/cc%d+)$")
    if cpu == nil or cpu == "" then
        cpu, accel = body:match("^(.-)/(accel/amd/gfx[0-9a-f]+)$")
    end
    if cpu ~= nil and cpu ~= "" and accel ~= nil then
        return cpu, accel
    end
    return body, nil
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

-- The environment wins when it is set. Otherwise the conf key, which is
-- unset when the text has no hide_toolchains value.
function M.effective_spec(env_value, conf_text)
    local from_env = M.normal_spec(env_value)
    if from_env ~= nil then
        return from_env
    end
    return M.normal_spec(M.conf_value(conf_text, "hide_toolchains"))
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
            return text
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
end

if type(hook) == "table" and type(hook.register) == "function" then
    -- The third argument appends, since Lmod 8.7.35. An older Lmod rejects it.
    local function register(name, fn)
        if not pcall(hook.register, name, fn, "append") then
            hook.register(name, fn)
        end
    end
    register("load", M.load_hook)
    register("isVisibleHook", M.is_visible_hook)
end

return M
