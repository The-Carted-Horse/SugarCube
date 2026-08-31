/*
 * What every data source has been doing, so a person can see whether it is
 * flowing without opening a serial console.
 *
 * glucocube/synclog.py's ring buffer, and its reasoning: restarts clear it,
 * which is fine for a health view. Nothing here is persisted — a log worth
 * keeping across a reboot would be a log worth writing to flash, and this
 * one is read while standing in front of the thing.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* synclog.py keeps 400. That is a lot of PSRAM here for something nobody
 * scrolls: an hour of four sources failing every five minutes is under 50
 * entries, and the page shows the newest first. */
#define GC_SYNCLOG_ENTRIES 96
#define GC_SYNCLOG_SOURCE 16
#define GC_SYNCLOG_USER 48
#define GC_SYNCLOG_MESSAGE 128

typedef struct {
    int64_t ms;                          /* wall clock, 0 before SNTP lands */
    char source[GC_SYNCLOG_SOURCE];      /* "nightscout", "network", ... */
    char user[GC_SYNCLOG_USER];          /* whose, or "system" */
    char message[GC_SYNCLOG_MESSAGE];
    bool ok;
} gc_synclog_entry_t;

/* Safe to call from any task, and before gc_synclog_init — an event that
 * happens during start-up is exactly the one worth having. */
void gc_synclog_add(const char *source, const char *user, bool ok,
                    const char *fmt, ...) __attribute__((format(printf, 4, 5)));

/* Copies up to `limit` entries, newest first. Returns how many. */
int gc_synclog_recent(gc_synclog_entry_t *out, int limit);

#ifdef __cplusplus
}
#endif
