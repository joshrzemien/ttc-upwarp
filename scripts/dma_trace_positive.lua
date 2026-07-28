local MARIO_Y = 0x80339E40
local SI_DRAM_ADDR = 0xA4800000
local SI_PIF_ADDR_RD64B = 0xA4800004
local PI_DRAM_ADDR = 0xA4600000
local PI_CART_ADDR = 0xA4600004
local PI_WR_LEN = 0xA460000C
local SP_DMEM = 0xA4000000
local SP_MEM_ADDR = 0xA4040000
local SP_DRAM_ADDR = 0xA4040004
local SP_WR_LEN = 0xA404000C

local engine = assert(os.getenv("DMA_ENGINE"), "DMA_ENGINE is required")
local ran = false

function Fuzzer.start(_) end

function Fuzzer.get_inputs(_, inputs)
    inputs:setRaw(0)
end

function Fuzzer.update(_)
    if ran then return end
    ran = true
    Fuzzer:setUInt(MARIO_Y, 0xC5837800)
    local old_word = Fuzzer:getUInt(MARIO_Y)

    if engine == "SI" then
        Fuzzer:setUInt(SI_DRAM_ADDR, 0x00339E40)
        Fuzzer:setUInt(SI_PIF_ADDR_RD64B, 0x1FC007C0)
    elseif engine == "PI" then
        Fuzzer:setUInt(PI_DRAM_ADDR, 0x00339E40)
        Fuzzer:setUInt(PI_CART_ADDR, 0x10000000)
        Fuzzer:setUInt(PI_WR_LEN, 3)
    elseif engine == "SP" then
        Fuzzer:setUInt(SP_DMEM, 0xC4837800)
        Fuzzer:setUInt(SP_DMEM + 4, old_word)
        Fuzzer:setUInt(SP_MEM_ADDR, 0)
        Fuzzer:setUInt(SP_DRAM_ADDR, 0x00339E40)
        Fuzzer:setUInt(SP_WR_LEN, 0)
    else
        error("DMA_ENGINE must be SI, PI, or SP")
    end

    print(string.format(
        "DMA_POSITIVE,engine=%s,old=%08X,new=%08X",
        engine,
        old_word,
        Fuzzer:getUInt(MARIO_Y)
    ))
    io.stdout:flush()
    os.exit(0)
end
