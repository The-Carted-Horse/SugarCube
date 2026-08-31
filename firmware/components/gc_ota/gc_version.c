/*
 * updater.py's parse_version and its ordering, in C. See gc_version.h.
 */

#include "gc_version.h"

#include <ctype.h>
#include <string.h>

/* ------------------------------------------------------------ versions -- */
/*
 * updater.py's parse_version, in C. 'v1.2.3' -> ((1,2,3), RELEASE, 0, 0)
 * and 'v1.2.3-rc.2' -> ((1,2,3), PRERELEASE, 3, 2). The tail is what makes
 * a finished version outrank every pre-release of the same number, so a
 * device on 2.0.1-rc.2 is offered the real 2.0.1 when it lands.
 *
 * A string this cannot read never compares as newer, which is the safe
 * direction for something that replaces the running code.
 */

#define VERSION_PARTS 4

typedef struct {
    bool ok;
    int nums[VERSION_PARTS];
    int stage;   /* 0 pre-release, 1 finished */
    int rank;    /* alpha < pre < beta < rc */
    int number;
} version_t;

static const struct {
    const char *label;
    int rank;
} PRE_RANKS[] = {
    {"alpha", 0}, {"pre", 1}, {"beta", 2}, {"rc", 3},
};

/* Case-insensitive prefix match, spelled out rather than reached for:
 * strncasecmp is POSIX, and this file is built by both a host compiler in
 * strict C11 and the device toolchain, where the two disagree about
 * whether it exists. */
static bool label_matches(const char **cursor, const char *label)
{
    const char *text = *cursor;
    for (; *label != '\0'; label++, text++) {
        if (tolower((unsigned char)*text) != tolower((unsigned char)*label)) {
            return false;
        }
    }
    *cursor = text;
    return true;
}

static version_t parse_version(const char *text)
{
    version_t out = {0};
    if (text == NULL) {
        return out;
    }
    while (*text == ' ' || *text == '\t') {
        text++;
    }
    if (*text == 'v' || *text == 'V') {
        text++;
    }
    if (!isdigit((unsigned char)*text)) {
        return out;
    }

    int part = 0;
    while (isdigit((unsigned char)*text)) {
        int value = 0;
        while (isdigit((unsigned char)*text)) {
            value = value * 10 + (*text++ - '0');
        }
        if (part < VERSION_PARTS) {
            out.nums[part] = value;
        }
        part++;
        if (*text == '.' && isdigit((unsigned char)text[1])) {
            text++;
            continue;
        }
        break;
    }

    out.stage = 1;   /* finished, unless a pre-release label follows */
    if (*text == '\0') {
        out.ok = true;
        return out;
    }

    /* The separator before the label is optional and may be any of -_. */
    const char *cursor = text;
    if (*cursor == '-' || *cursor == '_' || *cursor == '.') {
        cursor++;
    }
    for (size_t i = 0; i < sizeof(PRE_RANKS) / sizeof(PRE_RANKS[0]); i++) {
        const char *after = cursor;
        if (!label_matches(&after, PRE_RANKS[i].label)) {
            continue;
        }
        out.stage = 0;
        out.rank = PRE_RANKS[i].rank;
        if (*after == '-' || *after == '_' || *after == '.') {
            after++;
        }
        out.number = 0;
        while (isdigit((unsigned char)*after)) {
            out.number = out.number * 10 + (*after++ - '0');
        }
        /* Anything left over is not a version this device can reason
         * about, and reasoning about it wrongly replaces running code. */
        out.ok = (*after == '\0');
        return out;
    }
    return out;
}

/* Negative, zero or positive, like strcmp. (1, 0) and (1, 0, 0) compare
 * equal because the unset parts are already zero. */
static int compare_versions(const version_t *a, const version_t *b)
{
    for (int i = 0; i < VERSION_PARTS; i++) {
        if (a->nums[i] != b->nums[i]) {
            return a->nums[i] < b->nums[i] ? -1 : 1;
        }
    }
    if (a->stage != b->stage) {
        return a->stage < b->stage ? -1 : 1;
    }
    if (a->rank != b->rank) {
        return a->rank < b->rank ? -1 : 1;
    }
    if (a->number != b->number) {
        return a->number < b->number ? -1 : 1;
    }
    return 0;
}

bool gc_ota_version_is_newer(const char *candidate, const char *current)
{
    const version_t a = parse_version(candidate);
    const version_t b = parse_version(current);
    if (!a.ok || !b.ok) {
        return false;
    }
    return compare_versions(&a, &b) > 0;
}

bool gc_ota_version_is_prerelease(const char *version)
{
    const version_t parsed = parse_version(version);
    return parsed.ok && parsed.stage == 0;
}

