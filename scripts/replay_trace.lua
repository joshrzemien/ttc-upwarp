local MARIO = 0x80339E00
local movie = nil
local vi = 0
local polls = 0
local done_vis = 0
local max_vis = tonumber(os.getenv("MAX_VIS") or "240")
local trace_every = tonumber(os.getenv("TRACE_EVERY") or "1")

local function u32(address)
    return Fuzzer:getUInt(address)
end

local function f32(address)
    return Fuzzer:getFloat(address)
end

function Fuzzer.start(_)
    local path = assert(os.getenv("M64_PATH"), "M64_PATH is required")
    movie = assert(Fuzzer:openM64(path), "could not open M64 movie: " .. path)
    print("TRACE_HEADER,vi,polls,input,action,x,y,z,vx,vy,vz,forward_vel,ceil_height,floor_height,wall,ceil,floor")
    io.stdout:flush()
end

function Fuzzer.get_inputs(_, inputs)
    local raw = movie:getNextInput()
    inputs:setRaw(raw)
    polls = polls + 1
end

function Fuzzer.update(_)
    vi = vi + 1
    local input_position = movie:getPosition()
    if vi <= 40 or vi % trace_every == 0 then
        print(string.format(
            "TRACE,%d,%d,%d,%08X,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%08X,%08X,%08X",
            vi,
            polls,
            input_position,
            u32(MARIO + 0x0C),
            f32(MARIO + 0x3C),
            f32(MARIO + 0x40),
            f32(MARIO + 0x44),
            f32(MARIO + 0x48),
            f32(MARIO + 0x4C),
            f32(MARIO + 0x50),
            f32(MARIO + 0x54),
            f32(MARIO + 0x6C),
            f32(MARIO + 0x70),
            u32(MARIO + 0x60),
            u32(MARIO + 0x64),
            u32(MARIO + 0x68)
        ))
    end
    io.stdout:flush()

    if movie:isFinished() then
        done_vis = done_vis + 1
    end
    if done_vis >= 12 or vi >= max_vis then
        print(string.format("TRACE_DONE,vi=%d,polls=%d,input=%d,finished=%s", vi, polls, input_position, tostring(movie:isFinished())))
        io.stdout:flush()
        os.exit(0)
    end
end
