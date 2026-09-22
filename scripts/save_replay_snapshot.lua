local MARIO = 0x80339E00
local GLOBAL_TIMER = 0x8032C694

local movie = nil
local vi = 0
local polls = 0
local saved = false
local save_vi = tonumber(os.getenv("SAVE_VI") or "0")
local save_poll = tonumber(os.getenv("SAVE_POLL") or "0")
local post_save_vis = tonumber(os.getenv("POST_SAVE_VIS") or "60")
local remaining_vis = nil

local function u32(address)
    return Fuzzer:getUInt(address)
end

local function f32(address)
    return Fuzzer:getFloat(address)
end

local function should_save()
    if save_vi > 0 and vi == save_vi then
        return true
    end
    if save_poll > 0 and polls == save_poll then
        return true
    end
    return false
end

function Fuzzer.start(_)
    local movie_path = assert(os.getenv("M64_PATH"), "M64_PATH is required")
    movie = assert(Fuzzer:openM64(movie_path), "could not open M64 movie: " .. movie_path)
    assert(save_vi > 0 or save_poll > 0, "SAVE_VI or SAVE_POLL must be positive")
end

function Fuzzer.get_inputs(_, inputs)
    inputs:setRaw(movie:getNextInput())
    polls = polls + 1
end

function Fuzzer.update(_)
    vi = vi + 1
    if not saved and should_save() then
        local save_path = assert(os.getenv("SAVE_PATH"), "SAVE_PATH is required")
        Fuzzer:saveState(save_path)
        saved = true
        remaining_vis = post_save_vis
        print(string.format(
            "SNAPSHOT_REQUEST,path=%s,vi=%d,polls=%d,timer=%d,input=%d,action=%08X,x=%.9g,y=%.9g,z=%.9g,vx=%.9g,vy=%.9g,vz=%.9g,floor_height=%.9g,ceil_height=%.9g",
            save_path,
            vi,
            polls,
            u32(GLOBAL_TIMER),
            movie:getPosition(),
            u32(MARIO + 0x0C),
            f32(MARIO + 0x3C),
            f32(MARIO + 0x40),
            f32(MARIO + 0x44),
            f32(MARIO + 0x48),
            f32(MARIO + 0x4C),
            f32(MARIO + 0x50),
            f32(MARIO + 0x70),
            f32(MARIO + 0x6C)
        ))
        io.stdout:flush()
    elseif saved then
        remaining_vis = remaining_vis - 1
        if remaining_vis <= 0 then
            print(string.format("SNAPSHOT_DONE,vi=%d,polls=%d,timer=%d", vi, polls, u32(GLOBAL_TIMER)))
            io.stdout:flush()
            Fuzzer:stop()
        end
    end
end
