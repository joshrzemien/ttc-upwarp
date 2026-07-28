local MARIO = 0x80339E00
local PLACE_VI = tonumber(os.getenv("PLACE_VI") or "100")
local FLIP_VI = tonumber(os.getenv("FLIP_VI") or "101")
local STOP_VI = tonumber(os.getenv("STOP_VI") or "170")
local DO_FLIP = os.getenv("INJECT_BIT_FLIP") ~= "0"
local X = tonumber(os.getenv("EVENT_X") or "800")
local Z = tonumber(os.getenv("EVENT_Z") or "1900")
local SOURCE_Y_WORD = 0xC5837800
local FLIPPED_Y_WORD = 0xC4837800
local vi = 0
local movie

local function u32(address)
    return Fuzzer:getUInt(address)
end

local function f32(address)
    return Fuzzer:getFloat(address)
end

local function set_position(x, y, z)
    Fuzzer:setFloat(MARIO + 0x3C, x)
    Fuzzer:setFloat(MARIO + 0x40, y)
    Fuzzer:setFloat(MARIO + 0x44, z)
    local object = u32(MARIO + 0x88)
    if object >= 0x80000000 and object < 0x80800000 then
        Fuzzer:setFloat(object + 0xA0, x)
        Fuzzer:setFloat(object + 0xA4, y)
        Fuzzer:setFloat(object + 0xA8, z)
    end
end

function Fuzzer.start(_)
    local path = assert(os.getenv("M64_PATH"), "M64_PATH is required")
    movie = assert(Fuzzer:openM64(path), "could not open M64 movie: " .. path)
    assert(FLIP_VI > PLACE_VI, "FLIP_VI must follow PLACE_VI")
    print("BITFLIP_HEADER,vi,event,action,y_word,x,y,z,vy,floor,ceil")
end

function Fuzzer.get_inputs(_, inputs)
    inputs:setRaw(movie:getNextInput())
end

function Fuzzer.update(_)
    vi = vi + 1
    local event = "tick"

    if vi == PLACE_VI then
        Fuzzer:setUInt(MARIO + 0x0C, 0x0100088C) -- ACT_FREEFALL
        Fuzzer:setFloat(MARIO + 0x48, 0)
        Fuzzer:setFloat(MARIO + 0x4C, -20)
        Fuzzer:setFloat(MARIO + 0x50, 0)
        Fuzzer:setFloat(MARIO + 0x54, 0)
        set_position(X, -4207, Z)
        assert(u32(MARIO + 0x40) == SOURCE_Y_WORD, "source Y did not encode as C5837800")
        event = "place_C5837800"
    elseif vi == FLIP_VI and DO_FLIP then
        local before = u32(MARIO + 0x40)
        assert(before == SOURCE_Y_WORD, string.format("unexpected pre-flip Y word: %08X", before))
        Fuzzer:setUInt(MARIO + 0x40, before - 0x01000000)
        assert(u32(MARIO + 0x40) == FLIPPED_Y_WORD, "bit 24 did not clear to C4837800")
        event = "clear_bit_24"
    end

    if vi >= PLACE_VI - 4 then
        print(string.format(
            "BITFLIP,%d,%s,%08X,%08X,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g",
            vi, event, u32(MARIO + 0x0C), u32(MARIO + 0x40),
            f32(MARIO + 0x3C), f32(MARIO + 0x40), f32(MARIO + 0x44),
            f32(MARIO + 0x4C), f32(MARIO + 0x70), f32(MARIO + 0x6C)))
        io.stdout:flush()
    end

    if vi >= STOP_VI then
        os.exit(0)
    end
end
