#ifndef WAFEL_JP_DLL_H
#define WAFEL_JP_DLL_H

#include <windows.h>
#include <bcrypt.h>
#include <stdio.h>
#include <string.h>

/* The harnesses use private RVAs, x64 structure offsets, and instruction bytes
 * from this exact image. A fresh build or another JP DLL is not interchangeable.
 * Link with -lbcrypt. CNG hashing does not require an installed RSA provider. */
#define WAFEL_JP_DLL_SHA256 \
    "a3dc4984628bfc2bcdc92eb2c3af47beae1472fd54c07a24c286046d7962f67b"

_Static_assert(sizeof(void *) == 8, "Wafel harnesses require a Win64 compiler");

static HMODULE load_wafel_jp_dll(const char *path) {
    char absolute_path[32768];
    DWORD path_length = GetFullPathNameA(path, sizeof(absolute_path),
                                        absolute_path, NULL);
    if (path_length == 0 || path_length >= sizeof(absolute_path)) {
        fprintf(stderr, "invalid DLL path: %s\n", path);
        return NULL;
    }

    /* Deny writes/deletion while hashing and loading the same resolved file. */
    HANDLE file = CreateFileA(absolute_path, GENERIC_READ, FILE_SHARE_READ, NULL,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    BCRYPT_ALG_HANDLE provider = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    HMODULE module = NULL;
    DWORD error = ERROR_SUCCESS;
    NTSTATUS status = 0;
    if (file == INVALID_HANDLE_VALUE) {
        error = GetLastError();
        goto done;
    }
    if ((status = BCryptOpenAlgorithmProvider(&provider, BCRYPT_SHA256_ALGORITHM,
                                               NULL, 0)) < 0
        || (status = BCryptCreateHash(provider, &hash, NULL, 0, NULL, 0, 0)) < 0) {
        goto done;
    }
    BYTE buffer[65536];
    DWORD count;
    for (;;) {
        if (!ReadFile(file, buffer, sizeof(buffer), &count, NULL)) {
            error = GetLastError();
            goto done;
        }
        if (count == 0) {
            break;
        }
        if ((status = BCryptHashData(hash, buffer, count, 0)) < 0) {
            goto done;
        }
    }
    BYTE digest[32];
    if ((status = BCryptFinishHash(hash, digest, sizeof(digest), 0)) < 0) {
        goto done;
    }
    char hex[65];
    for (size_t i = 0; i < sizeof(digest); i++) {
        static const char digits[] = "0123456789abcdef";
        hex[2 * i] = digits[digest[i] >> 4];
        hex[2 * i + 1] = digits[digest[i] & 15];
    }
    hex[64] = '\0';
    if (strcmp(hex, WAFEL_JP_DLL_SHA256) != 0) {
        fprintf(stderr,
                "unsupported Wafel DLL: %s\nexpected SHA256 %s\nactual   SHA256 %s\n"
                "Unlock the pinned JP artifact with scripts/setup_wafel.sh; "
                "private RVAs cannot be used with another build.\n",
                absolute_path, WAFEL_JP_DLL_SHA256, hex);
        goto done;
    }
    module = LoadLibraryA(absolute_path);
    if (module == NULL) {
        error = GetLastError();
    }

done:
    if (hash != NULL) {
        BCryptDestroyHash(hash);
    }
    if (provider != NULL) {
        BCryptCloseAlgorithmProvider(provider, 0);
    }
    if (file != INVALID_HANDLE_VALUE) {
        CloseHandle(file);
    }
    if (error != ERROR_SUCCESS) {
        fprintf(stderr, "cannot verify/load DLL %s (Windows error %lu)\n",
                absolute_path, error);
    }
    if (status < 0) {
        fprintf(stderr, "cannot hash DLL %s (BCrypt status 0x%08lx)\n",
                absolute_path, (unsigned long)status);
    }
    return module;
}

#endif
