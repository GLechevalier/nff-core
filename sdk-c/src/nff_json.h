/**
 * nff_json.h — minimal flat-object JSON field extractor (internal).
 *
 * Not a full JSON parser. Handles flat objects with string and integer
 * values only — exactly the command payload format. No allocations.
 * Implemented as static inline functions for code-size efficiency.
 */
#ifndef NFF_JSON_H
#define NFF_JSON_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Find the first occurrence of "key": in json, return pointer to value
   start (first non-whitespace after the colon), or NULL if not found. */
static inline const char *nff_json_find_value(const char *json, size_t json_len,
                                               const char *key) {
    size_t klen = strlen(key);
    const char *end = json + json_len;
    const char *p = json;

    while (p + klen + 3 < end) {
        /* Search for  "key"  */
        if (*p == '"' && memcmp(p + 1, key, klen) == 0 && *(p + 1 + klen) == '"') {
            /* Skip to colon */
            const char *q = p + 1 + klen + 1;
            while (q < end && (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r')) q++;
            if (q < end && *q == ':') {
                q++;
                while (q < end && (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r')) q++;
                if (q < end) return q;
            }
        }
        p++;
    }
    return NULL;
}

/**
 * Extract a string value into out (null-terminated, at most out_len-1 chars).
 * Returns 0 on success, -1 if not found or out_len too small.
 */
static inline int nff_json_get_str(const char *json, size_t json_len,
                                    const char *key,
                                    char *out, size_t out_len) {
    const char *v = nff_json_find_value(json, json_len, key);
    if (!v || *v != '"') return -1;
    v++; /* skip opening quote */
    size_t i = 0;
    while (*v != '"' && *v != '\0' && i + 1 < out_len) {
        if (*v == '\\') {
            v++;
            if (*v == '\0') break;
            /* Handle common escapes */
            switch (*v) {
                case '"':  out[i++] = '"';  break;
                case '\\': out[i++] = '\\'; break;
                case 'n':  out[i++] = '\n'; break;
                case 't':  out[i++] = '\t'; break;
                default:   out[i++] = *v;   break;
            }
        } else {
            out[i++] = *v;
        }
        v++;
    }
    out[i] = '\0';
    return (*v == '"') ? 0 : -1;
}

/**
 * Extract an unsigned 32-bit integer value.
 * Returns 0 on success, -1 if not found.
 */
static inline int nff_json_get_u32(const char *json, size_t json_len,
                                    const char *key, uint32_t *out) {
    const char *v = nff_json_find_value(json, json_len, key);
    if (!v) return -1;
    /* Skip leading whitespace already done in find_value */
    if (*v < '0' || *v > '9') return -1;
    uint32_t val = 0;
    while (*v >= '0' && *v <= '9') {
        val = val * 10 + (uint32_t)(*v - '0');
        v++;
    }
    *out = val;
    return 0;
}

/**
 * Extract a signed 32-bit integer value.
 * Returns 0 on success, -1 if not found.
 */
static inline int nff_json_get_i32(const char *json, size_t json_len,
                                    const char *key, int32_t *out) {
    const char *v = nff_json_find_value(json, json_len, key);
    if (!v) return -1;
    int sign = 1;
    if (*v == '-') { sign = -1; v++; }
    if (*v < '0' || *v > '9') return -1;
    int32_t val = 0;
    while (*v >= '0' && *v <= '9') {
        val = val * 10 + (int32_t)(*v - '0');
        v++;
    }
    *out = sign * val;
    return 0;
}

#ifdef __cplusplus
}
#endif

#endif /* NFF_JSON_H */
