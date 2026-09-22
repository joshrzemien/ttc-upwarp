#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "wafel_jp_dll.h"

typedef void (*game_fn)(void);

static FARPROC require_symbol(HMODULE module, const char *name) {
    FARPROC symbol = GetProcAddress(module, name);
    if (symbol == NULL) {
        fprintf(stderr, "missing symbol: %s (error %lu)\n", name, GetLastError());
        ExitProcess(2);
    }
    return symbol;
}

static uint16_t load_u16(const uint8_t *p) {
    uint16_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static uint32_t load_u32(const uint8_t *p) {
    uint32_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static float load_f32(const uint8_t *p) {
    float value;
    memcpy(&value, p, sizeof(value));
    return value;
}

int main(int argc, char **argv) {
    const char *module_path = argc > 1 ? argv[1] : "sm64_jp.dll";
    int frames = argc > 2 ? atoi(argv[2]) : 240;
    HMODULE module = load_wafel_jp_dll(module_path);
    if (module == NULL) {
        return 1;
    }

    game_fn init = (game_fn)require_symbol(module, "sm64_init");
    game_fn update = (game_fn)require_symbol(module, "sm64_update");
    game_fn init_mario_from_save_file =
        (game_fn)require_symbol(module, "init_mario_from_save_file");
    void **current_cmd = (void **)((uint8_t *)module + 12737304);
    void *ttc_entry = (void *)require_symbol(module, "level_ttc_entry");
    int16_t *level = (int16_t *)require_symbol(module, "gCurrLevelNum");
    int16_t *area = (int16_t *)require_symbol(module, "gCurrAreaIndex");
    int16_t *act = (int16_t *)require_symbol(module, "gCurrActNum");
    int16_t *ttc_speed = (int16_t *)require_symbol(module, "gTTCSpeedSetting");
    uint32_t *timer = (uint32_t *)require_symbol(module, "gGlobalTimer");
    uint8_t *mario = (uint8_t *)require_symbol(module, "gMarioStates");
    void **mario_object = (void **)require_symbol(module, "gMarioObject");
    void **current_area = (void **)require_symbol(module, "gCurrentArea");
    uint32_t *time_stop = (uint32_t *)require_symbol(module, "gTimeStopState");
    int16_t *play_mode = (int16_t *)((uint8_t *)module + 12737492);
    int8_t *debug_level_select =
        (int8_t *)require_symbol(module, "gDebugLevelSelect");

    init();
    init_mario_from_save_file();
    *level = 14;
    *area = 1;
    *act = 4;
    *ttc_speed = 3;
    *debug_level_select = 1;
    *current_cmd = ttc_entry;

    printf("TTC_PROBE_HEADER frame,timer,cmd_rva,current_area,mario_object,obj_flags,obj_bhv_rva,time_stop,play_mode,level,area,act,ttc_speed,action,action_state,action_timer,x,y,z,vx,vy,vz,floor_height\n");
    for (int frame = 0; frame < frames; frame++) {
        update();
        printf("TTC_PROBE %d,%" PRIu32 ",%" PRIXPTR ",%" PRIXPTR ",%" PRIXPTR ",%04X,%" PRIXPTR ",%08" PRIX32 ",%d,%d,%d,%d,%d,%08" PRIX32 ",%u,%u,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g\n",
               frame, *timer,
               (uintptr_t)*current_cmd - (uintptr_t)module,
               (uintptr_t)*current_area - (uintptr_t)module,
               (uintptr_t)*mario_object - (uintptr_t)module,
               load_u16((uint8_t *)*mario_object + 180),
               *(uintptr_t *)((uint8_t *)*mario_object + 1184) - (uintptr_t)module,
               *time_stop, *play_mode, *level, *area, *act, *ttc_speed,
               load_u32(mario + 12), load_u16(mario + 24), load_u16(mario + 26),
               load_f32(mario + 60), load_f32(mario + 64), load_f32(mario + 68),
               load_f32(mario + 72), load_f32(mario + 76), load_f32(mario + 80),
               load_f32(mario + 124));
    }

    FreeLibrary(module);
    return 0;
}
