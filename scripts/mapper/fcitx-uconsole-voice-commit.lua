local fcitx = require("fcitx")
local ime = require("imeapi")

local commit_file = os.getenv("HOME") .. "/.local/state/uconsole-helper-mapper/fcitx-voice-commit.txt"

local function read_all(path)
    local file = io.open(path, "rb")
    if file == nil then
        return nil
    end
    local content = file:read("*a")
    file:close()
    return content
end

function uconsole_voice_commit(_)
    local text = read_all(commit_file)
    if text == nil or #text == 0 then
        return nil
    end
    local file = io.open(commit_file, "wb")
    if file ~= nil then
        file:close()
    end
    fcitx.commitString(text)
    return ""
end

ime.register_command("uv", "uconsole_voice_commit", "uConsole voice commit", "none", "")
