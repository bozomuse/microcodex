// SPDX-FileCopyrightText: 2026 Paolo Anzani
// SPDX-License-Identifier: Apache-2.0

#include "allocator-memory.h"

// features.h defines __GLIBC__ when Linux is using glibc. Keeping the malloc
// headers behind platform checks also allows Linux builds using another libc.
#if defined(__linux__)
#include <features.h>
#endif

#if defined(__APPLE__)
#include <malloc/malloc.h>
#elif defined(__GLIBC__)
#include <malloc.h>
#endif

namespace microcodex {

    void configureAllocatorForLowMemory() noexcept {
#if defined(__APPLE__)
        // Darwin's allocator has no arena-count setting analogous to glibc's.
#elif defined(__GLIBC__)
        // MicroCodex normally has one UI thread plus a small number of workers.
        // A single arena favors a lower retained heap over allocation throughput.
        (void)::mallopt(M_ARENA_MAX, 1);
#endif
    }

    bool releaseUnusedHeap() noexcept {
#if defined(__APPLE__)
        // A null zone examines every malloc zone; a zero goal requests the
        // maximum pressure relief currently available.
        return ::malloc_zone_pressure_relief(nullptr, 0) != 0;
#elif defined(__GLIBC__)
        return ::malloc_trim(0) != 0;
#else
        return false;
#endif
    }

} // namespace microcodex
