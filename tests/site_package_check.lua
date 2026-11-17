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

if failed then
    os.exit(1)
end
print("ok")
