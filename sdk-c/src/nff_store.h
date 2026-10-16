/* nff_store.h — persistent storage of operational credentials in NVS (claim rollover).
 *
 * After a device is accepted, the fleet rolls over a UNIQUE per-device certificate to it
 * (DEVICE_OWNERSHIP_DESIGN.md §8). That cert/key/chain is persisted here, in NVS, so it survives
 * reboots and firmware OTAs — the firmware-baked bootstrap credential is only ever a fallback used
 * when NVS holds no operational creds (i.e. the device is still unclaimed).
 *
 * The NVS port is string-only (and the POSIX mock caps a value at 256 bytes), so DER blobs are
 * base64-encoded and split into <=200-char chunks across keys {prefix}.0..{prefix}.n with a count
 * at {prefix}.n. The single "claimed" flag is written LAST and is the sole gate, so a crash mid-write
 * leaves the device unclaimed (it re-enters bootstrap and overwrites the half-written creds).
 */
#ifndef NFF_STORE_H
#define NFF_STORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "nff.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Base64 (standard alphabet, padded). out must be large enough; returns bytes written or -1. */
int  nff_b64_encode(const uint8_t *in, size_t in_len, char *out, size_t out_cap);
int  nff_b64_decode(const char *in, size_t in_len, uint8_t *out, size_t out_cap);

/* True iff NVS holds a complete operational credential set (the "claimed" flag is set). */
bool nff_store_is_claimed(void);

/* Persist a per-device operational credential set. All blobs + project_id are committed, THEN the
 * claimed flag. Returns NFF_OK or a negative error. */
int  nff_store_save(const char *project_id,
                    const uint8_t *cert,  size_t cert_len,
                    const uint8_t *key,   size_t key_len,
                    const uint8_t *chain, size_t chain_len,
                    const uint8_t *ca,    size_t ca_len,
                    const uint8_t *vkey,  size_t vkey_len);

/* Load the stored operational creds into out (cert..vkey point at malloc'd buffers; project_id into
 * a malloc'd string). Scalar config (broker/fw/...) is inherited from base. Returns NFF_OK or error.
 * On success, the caller owns the buffers and must call nff_store_free_loaded() when done.
 * ca_cert may come back NULL/0: stores claimed before op_ca provisioning (or with an empty CA)
 * load fine without it — the caller must then supply its own trust anchor (e.g. a baked root CA). */
int  nff_store_load(const nff_config_t *base, nff_config_t *out);
void nff_store_free_loaded(nff_config_t *out);

#ifdef __cplusplus
}
#endif

#endif /* NFF_STORE_H */
