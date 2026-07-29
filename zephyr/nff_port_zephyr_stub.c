/**
 * nff_port_zephyr_stub.c — Zephyr V1 stub.
 *
 * All functions compile cleanly and return NFF_ERR_NOT_SUPPORTED.
 * This allows nff-sdk-c to be included in Zephyr projects without
 * build errors while the real Zephyr port (V2) is being developed.
 */

#if defined(CONFIG_NFF) || defined(__ZEPHYR__)

#include "nff_port.h"
#include <stddef.h>
#include <string.h>

uint32_t nff_port_millis(void)          { return 0; }
void     nff_port_delay_ms(uint32_t ms) { (void)ms; }

nff_mutex_t nff_port_mutex_create(void)        { return NULL; }
void nff_port_mutex_lock(nff_mutex_t m)        { (void)m; }
void nff_port_mutex_unlock(nff_mutex_t m)      { (void)m; }
void nff_port_mutex_destroy(nff_mutex_t m)     { (void)m; }

void nff_port_task_create(void (*fn)(void *), const char *name,
                           uint32_t s, void *a, uint8_t p, int c) {
    (void)fn;(void)name;(void)s;(void)a;(void)p;(void)c;
}

nff_mqtt_handle_t *nff_port_mqtt_create(void) { return NULL; }
void nff_port_mqtt_set_server(nff_mqtt_handle_t *h, const char *host, uint16_t p) { (void)h;(void)host;(void)p; }
void nff_port_mqtt_set_tls(nff_mqtt_handle_t *h, const uint8_t *a, size_t al, const uint8_t *b, size_t bl, const uint8_t *c, size_t cl) { (void)h;(void)a;(void)al;(void)b;(void)bl;(void)c;(void)cl; }
void nff_port_mqtt_set_skip_cn_check(nff_mqtt_handle_t *h, bool skip) { (void)h; (void)skip; }
void nff_port_mqtt_set_rx_callback(nff_mqtt_handle_t *h, void(*cb)(const char*,const uint8_t*,size_t,void*), void *u) { (void)h;(void)cb;(void)u; }
int  nff_port_mqtt_connect(nff_mqtt_handle_t *h, const char *i, const char *t, const char *p) { (void)h;(void)i;(void)t;(void)p; return NFF_ERR_NOT_SUPPORTED; }
int  nff_port_mqtt_subscribe(nff_mqtt_handle_t *h, const char *t, int q) { (void)h;(void)t;(void)q; return NFF_ERR_NOT_SUPPORTED; }
int  nff_port_mqtt_publish(nff_mqtt_handle_t *h, const char *t, const char *p, int q, bool r) { (void)h;(void)t;(void)p;(void)q;(void)r; return NFF_ERR_NOT_SUPPORTED; }
void nff_port_mqtt_loop(nff_mqtt_handle_t *h)            { (void)h; }
bool nff_port_mqtt_is_connected(nff_mqtt_handle_t *h)    { (void)h; return false; }

int  nff_port_nvs_set_str(const char *k, const char *v)          { (void)k;(void)v; return NFF_ERR_NOT_SUPPORTED; }
int  nff_port_nvs_get_str(const char *k, char *o, size_t l)      { (void)k;(void)o;(void)l; return NFF_ERR_NOT_SUPPORTED; }
int  nff_port_nvs_set_u32(const char *k, uint32_t v)             { (void)k;(void)v; return NFF_ERR_NOT_SUPPORTED; }
int  nff_port_nvs_get_u32(const char *k, uint32_t *o)            { (void)k;(void)o; return NFF_ERR_NOT_SUPPORTED; }
int  nff_port_nvs_erase_key(const char *k)                       { (void)k; return NFF_ERR_NOT_SUPPORTED; }
int  nff_port_nvs_commit(void)                                    { return NFF_ERR_NOT_SUPPORTED; }

nff_ota_handle_t nff_port_ota_begin(size_t s) { (void)s; return NFF_OTA_INVALID_HANDLE; }
int  nff_port_ota_write(nff_ota_handle_t h, const uint8_t *b, size_t l) { (void)h;(void)b;(void)l; return NFF_ERR_NOT_SUPPORTED; }
int  nff_port_ota_end(nff_ota_handle_t h)   { (void)h; return NFF_ERR_NOT_SUPPORTED; }
void nff_port_ota_abort(nff_ota_handle_t h) { (void)h; }
int  nff_port_ota_repair_boot_partition(void) { return 0; }
void nff_port_reboot(void)                  {}

int nff_port_https_get_stream(const char *u, int(*cb)(const uint8_t*,size_t,void*), void *c, uint32_t t) {
    (void)u;(void)cb;(void)c;(void)t; return NFF_ERR_NOT_SUPPORTED;
}

int nff_port_ecdsa_p256_verify(const uint8_t *k, const uint8_t *m, size_t ml, const uint8_t *s, size_t sl) {
    (void)k;(void)m;(void)ml;(void)s;(void)sl; return NFF_ERR_NOT_SUPPORTED;
}

void nff_port_get_diag_info(nff_diag_info_t *out) {
    if (out) memset(out, 0, sizeof(*out));
}

void nff_port_get_hw_info(nff_hw_info_t *out) {
    if (out) memset(out, 0, sizeof(*out));  /* stub: device_type empty = unknown */
}

void nff_port_install_panic_hook(void (*fn)(const char *)) { (void)fn; }

int nff_port_stall_core(int id)   { (void)id; return NFF_ERR_NOT_SUPPORTED; }
int nff_port_unstall_core(int id) { (void)id; return NFF_ERR_NOT_SUPPORTED; }

/* SHA-256 stubs (used by nff_ota) */
typedef void *nff_sha256_ctx_t;
nff_sha256_ctx_t nff_port_sha256_new(void)                                        { return NULL; }
void             nff_port_sha256_update(nff_sha256_ctx_t c, const uint8_t *d, size_t l) { (void)c;(void)d;(void)l; }
void             nff_port_sha256_finish(nff_sha256_ctx_t c, uint8_t out[32])      { (void)c; if(out) memset(out,0,32); }
void             nff_port_sha256_free(nff_sha256_ctx_t c)                         { (void)c; }

#endif /* CONFIG_NFF || __ZEPHYR__ */
