// SPDX-FileCopyrightText: 2026 Paolo Anzani
// SPDX-License-Identifier: Apache-2.0

#pragma once

namespace microcodex {

    // Apply allocator settings before worker threads are started. Platforms
    // without a supported allocator tuning API leave the defaults unchanged.
    void configureAllocatorForLowMemory() noexcept;

    // Ask the platform allocator to return unused heap pages to the operating
    // system. Returns true when the allocator reports that memory was released.
    [[nodiscard]] bool releaseUnusedHeap() noexcept;

} // namespace microcodex
