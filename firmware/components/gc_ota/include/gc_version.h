/*
 * How two release version strings compare.
 *
 * Split out of gc_ota.c because this is the logic that decides whether a
 * device replaces its own firmware, and because it needs nothing from the
 * SDK — so firmware/host_test builds it with a host compiler and checks it
 * against glucocube/updater.py's answers for the same strings.
 */

#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* True when `candidate` is a release this device should move to. A string
 * either side cannot read never compares as newer, which is the safe
 * direction for something that replaces running code. */
bool gc_ota_version_is_newer(const char *candidate, const char *current);

/* True for v1.2.3-rc.1 and friends; false for v1.2.3 and for anything
 * unreadable. */
bool gc_ota_version_is_prerelease(const char *version);

#ifdef __cplusplus
}
#endif
