-- Warn when a module file under software/linux/SUBDIR/modules is loaded
-- and EESSI_SOFTWARE_SUBDIR names a different subdirectory.
-- The load continues. Lmod does not run this hook for a job that inherited
-- its modules; the TaskProlog still reloads that stack.

local M = {}

function M.software_subdir(fn)
    if type(fn) ~= "string" then
        return nil
    end
    return fn:match("/software/linux/(.-)/modules/")
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
    if not pcall(hook.register, "load", M.load_hook, "append") then
        hook.register("load", M.load_hook)
    end
end

return M
