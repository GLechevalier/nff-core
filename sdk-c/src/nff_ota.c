/**
 * nff_ota.c — HTTPS OTA firmware download with SHA-256 verification and rollback.
 *
 * Handles the "ota" command:
 *   1. Anti-downgrade version check
 *   2. HTTPS streaming download into OTA partition
 *   3. SHA-256 verification of downloaded image
 *   4. Boot partition switch + reboot
 *   5. On next boot: publish ota_result (committed/rolled_back) via NVS flag
 *
 * SHA-256 is computed incrementally during the download using mbedTLS
 * (linked by WiFi stack on all ESP targets; OpenSSL on POSIX).
 */

#include "nff_internal.h"
#include "nff_json.h"
#include <string.h>
#include <stdio.h>

/* ------------------------------------------------------------------ */
/* SHA-256 streaming state (platform-specific compute)                 */
/* ------------------------------------------------------------------ */

/* SHA-256 functions are declared in nff_port.h */

/* ------------------------------------------------------------------ */
/* Version comparison: "1.2.3" < "1.2.4" → true (anti-downgrade)      */
/* ------------------------------------------------------------------ */

static int version_parts(const char *v, int *maj, int *min, int *patch) {
    return (sscanf(v, "%d.%d.%d", maj, min, patch) == 3) ? 0 : -1;
}

static int version_le(const char *a, const char *b) {
    int a1=0, a2=0, a3=0, b1=0, b2=0, b3=0;
    if (version_parts(a, &a1, &a2, &a3) < 0) return 0;
    if (version_parts(b, &b1, &b2, &b3) < 0) return 0;
    if (a1 != b1) return (a1 < b1);
    if (a2 != b2) return (a2 < b2);
    return (a3 <= b3);
}

/* ------------------------------------------------------------------ */
/* Download context passed through chunk callback                       */
/* ------------------------------------------------------------------ */

typedef struct {
    nff_ota_handle_t  ota;
    nff_sha256_ctx_t  sha;
    size_t            written;
    int               error;
    /* Mid-download progress telemetry (see ota_publish_progress) */
    size_t            total;        /* expected image size; 0 = unknown */
    int               last_pct;     /* percent at last publish */
    uint32_t          last_pub_ms;  /* last publish/pump timestamp */
    int               attempt;      /* 1-based; bumped on a from-scratch restart */
    const char       *version;     /* wire version of the image being fetched */
    const char       *topic;       /* response topic (alive for the whole download) */
} ota_dl_ctx_t;

/* Publish one QoS-0, non-retained ota_progress tick. pct < 0 = size unknown:
   the "progress" field is omitted so the cloud treats it as a liveness keepalive
   only. attempt lets the server accept the percent going backwards after a
   from-scratch restart (resume unsupported). */
static void ota_publish_progress(const char *topic, const char *version,
                                 const char *stage, int pct,
                                 size_t written, size_t total, int attempt) {
    if (!nff_port_mqtt_is_connected(g_nff.mqtt)) return;
    char msg[256];
    if (pct >= 0) {
        snprintf(msg, sizeof(msg),
                 "{\"type\":\"ota_progress\",\"id\":\"%s\",\"version\":\"%s\","
                 "\"stage\":\"%s\",\"progress\":%d,"
                 "\"written\":%lu,\"size\":%lu,\"attempt\":%d}",
                 g_nff.cfg->device_id, version, stage, pct,
                 (unsigned long)written, (unsigned long)total, attempt);
    } else {
        snprintf(msg, sizeof(msg),
                 "{\"type\":\"ota_progress\",\"id\":\"%s\",\"version\":\"%s\","
                 "\"stage\":\"%s\",\"written\":%lu,\"attempt\":%d}",
                 g_nff.cfg->device_id, version, stage,
                 (unsigned long)written, attempt);
    }
    nff_port_mqtt_publish(g_nff.mqtt, topic, msg, 0, false);
}

static int ota_chunk_cb(const uint8_t *buf, size_t len, void *user_ctx) {
    ota_dl_ctx_t *ctx = (ota_dl_ctx_t *)user_ctx;
    if (ctx->error) return -1;

    nff_port_sha256_update(ctx->sha, buf, len);

    int rc = nff_port_ota_write(ctx->ota, buf, len);
    if (rc != 0) {
        ctx->error = rc;
        return -1;
    }
    ctx->written += len;

    /* The blocking download starves nff_mqtt_tick() for its whole (multi-minute)
       duration, so the MQTT client MUST be pumped here or keepalive dies and the
       broker drops the session mid-OTA. The pump runs on the MIN_MS cadence
       regardless of whether a progress tick also fires. */
    uint32_t now = nff_port_millis();
    if ((uint32_t)(now - ctx->last_pub_ms) >= NFF_OTA_PROGRESS_MIN_MS) {
        int pct = ctx->total
            ? (int)((uint64_t)ctx->written * 100u / ctx->total) : -1;
        if (pct < 0 || pct >= ctx->last_pct + NFF_OTA_PROGRESS_STEP_PCT) {
            ota_publish_progress(ctx->topic, ctx->version, "downloading", pct,
                                 ctx->written, ctx->total, ctx->attempt);
            if (pct > ctx->last_pct) ctx->last_pct = pct;
        }
        ctx->last_pub_ms = now;
        nff_port_mqtt_loop(g_nff.mqtt);
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* OTA result publish (called after MQTT reconnect on new firmware)    */
/* ------------------------------------------------------------------ */

void nff_ota_check_pending_result(void) {
    if (!g_nff.pending_ota_result) return;
    g_nff.pending_ota_result = false;

    char payload[256];
    snprintf(payload, sizeof(payload),
             "{\"type\":\"ota_result\","
             "\"status\":\"%s\","
             "\"version\":\"%s\","
             "\"id\":\"%s\"}",
             g_nff.pending_ota_committed ? "committed" : "rolled_back",
             g_nff.pending_ota_version,
             g_nff.cfg->device_id);

    char topic[NFF_TOPIC_MAXLEN];
    nff_topic_response(&g_nff, topic);
    nff_port_mqtt_publish(g_nff.mqtt, topic, payload, 1, false);
}

/* ------------------------------------------------------------------ */
/* Automatic rollback: trial / confirm / revert                         */
/* ------------------------------------------------------------------ */

/* Erase the probation record. Result keys (ota_pending/version/committed) are
   managed separately by commit (none) / rollback (sets them for the old image). */
static void ota_clear_trial_nvs(void) {
    nff_port_nvs_erase_key("ota_trial");
    nff_port_nvs_erase_key("ota_boot_count");
    nff_port_nvs_commit();
}

/* The trial image proved healthy: confirm it and report committed. */
static void nff_ota_commit(void) {
    nff_port_ota_mark_valid();          /* cancel any pending bootloader rollback */
    g_nff.ota_marked_valid = true;

    char payload[256];
    snprintf(payload, sizeof(payload),
             "{\"type\":\"ota_result\",\"status\":\"committed\","
             "\"version\":\"%s\",\"id\":\"%s\"}",
             g_nff.pending_ota_version, g_nff.cfg->device_id);
    char topic[NFF_TOPIC_MAXLEN];
    nff_topic_response(&g_nff, topic);
    nff_port_mqtt_publish(g_nff.mqtt, topic, payload, 1, false);

    /* Record the version of the image we just committed so the next config load
       (nvs_creds.c) reports the RUNNING image's version in telemetry, not the
       build-time-baked NFF_FW_VERSION of this generic L2 image. Written only on
       commit, so it always names the currently-booted, confirmed image — which is
       also the image a later rollback reverts to, so rollback must NOT clear it.
       Flushed by ota_clear_trial_nvs() below (which commits). */
    nff_port_nvs_set_str("fw_adopted", g_nff.pending_ota_version);

    ota_clear_trial_nvs();
    g_nff.ota_trial = false;
    nff_log("nff: OTA committed v%s", g_nff.pending_ota_version);
}

/* The trial image failed the health gate (or crash-looped): queue a rolled_back
   result for the reverted image to publish, then revert the boot partition. */
static void nff_ota_rollback(const char *reason) {
    /* The OLD image, after the revert reboot, reads these in nff_init and
       publishes rolled_back via the existing nff_ota_check_pending_result path. */
    nff_port_nvs_set_str("ota_pending",   "1");
    nff_port_nvs_set_str("ota_version",   g_nff.pending_ota_version);
    nff_port_nvs_set_str("ota_committed", "0");   /* 0 => rolled_back */
    ota_clear_trial_nvs();                         /* also commits */
    g_nff.ota_trial = false;
    nff_log("nff: OTA rollback v%s (%s) — reverting",
            g_nff.pending_ota_version, reason ? reason : "");
    nff_port_ota_rollback();   /* revert boot partition + reboot; no return on HW */
}

/* Called from nff_init. Detects a probation boot, arms the confirm window, or
   rolls back immediately if the new image keeps rebooting without confirming. */
void nff_ota_boot_check(void) {
    char trial[4] = {0};
    if (nff_port_nvs_get_str("ota_trial", trial, sizeof(trial)) != 0 || trial[0] != '1') {
        return;   /* not on probation */
    }

    /* Remember the trial version so commit/rollback can report it. */
    nff_port_nvs_get_str("ota_version", g_nff.pending_ota_version,
                         sizeof(g_nff.pending_ota_version));

    /* Bootloader fallback guard: the trial flag is set but the image running is NOT the
       trial — the bootloader rejected the freshly-flashed slot at boot and fell back to
       this (previous) image. Running probation here would falsely COMMIT an update this
       device isn't running (the platform then records success and a wrong version). The
       port has already repointed boot selection at this image; report rolled_back via the
       same pending-result keys nff_ota_rollback uses (nff_core reads them right after this
       call) and clear the trial. */
    if (nff_port_ota_repair_boot_partition() != 0) {
        nff_port_nvs_set_str("ota_pending",   "1");
        nff_port_nvs_set_str("ota_version",   g_nff.pending_ota_version);
        nff_port_nvs_set_str("ota_committed", "0");   /* 0 => rolled_back */
        ota_clear_trial_nvs();                         /* also commits */
        nff_log("nff: OTA trial v%s never booted (bootloader fallback) — reporting rolled_back",
                g_nff.pending_ota_version);
        return;
    }

    /* Crash-loop guard: every unconfirmed boot bumps the counter. */
    uint32_t boots = 0;
    nff_port_nvs_get_u32("ota_boot_count", &boots);
    boots++;
    nff_port_nvs_set_u32("ota_boot_count", boots);
    nff_port_nvs_commit();

    if (boots >= NFF_OTA_MAX_TRIAL_BOOTS) {
        nff_ota_rollback("crash loop");   /* reverts + reboots; no return on HW */
        return;
    }

    /* Arm probation; the health gate runs from nff_loop once connected. */
    g_nff.ota_trial = true;
    g_nff.ota_health_since_ms = 0;   /* soak starts when the image first reports healthy */
    g_nff.ota_trial_deadline_ms =
        nff_port_millis() + (uint32_t)NFF_OTA_CONFIRM_TIMEOUT_S * 1000u;
    nff_log("nff: OTA trial boot %lu/%d v%s", (unsigned long)boots,
            NFF_OTA_MAX_TRIAL_BOOTS, g_nff.pending_ota_version);
}

/* Built-in default health gate (IOT_BRIDGE.md §3 gap #7). Beyond "broker reconnects",
   require a minimum free-heap floor so an image that connects but is leaking heap is not
   committed. This is AND'd with any nff_register_health_check() callback — both must pass. */
static bool nff_ota_default_health_ok(void) {
    nff_diag_info_t d;
    nff_port_get_diag_info(&d);
    return d.min_free_heap >= (uint32_t)NFF_OTA_MIN_HEAP_FLOOR;
}

/* Called from nff_loop while on probation: commit only after the image holds healthy for the
   soak window (NFF_OTA_MIN_HEALTHY_MS), else roll back once the confirm window elapses. The
   soak catches an image that connects then degrades within seconds. */
void nff_ota_trial_tick(void) {
    if (!g_nff.ota_trial) return;

    /* NULL guard: with the tick no longer gated on CONNECTED this can run before
       the MQTT handle exists, and the Arduino ports dereference it unchecked. */
    bool connected = g_nff.mqtt && nff_port_mqtt_is_connected(g_nff.mqtt);
    bool healthy   = connected &&
                     nff_ota_default_health_ok() &&
                     (g_nff.health_cb == NULL || g_nff.health_cb(g_nff.health_user));

    if (healthy) {
        uint32_t now = nff_port_millis();
        if (g_nff.ota_health_since_ms == 0) {
            g_nff.ota_health_since_ms = now;   /* start the soak (0 reserved for "not yet") */
            if (g_nff.ota_health_since_ms == 0) g_nff.ota_health_since_ms = 1;
        }
        /* Signed compare tolerates nff_port_millis() wraparound. */
        if ((int32_t)(now - g_nff.ota_health_since_ms) >= (int32_t)NFF_OTA_MIN_HEALTHY_MS) {
            nff_ota_commit();
        }
        return;
    }

    /* Lost health (or never reached it): restart the soak so it must hold continuously. */
    g_nff.ota_health_since_ms = 0;

    /* Signed compare tolerates nff_port_millis() wraparound. */
    if ((int32_t)(nff_port_millis() - g_nff.ota_trial_deadline_ms) >= 0) {
        nff_ota_rollback("health check timeout");
    }
}

/* ------------------------------------------------------------------ */
/* Main OTA command handler (called from nff_cmd.c)                    */
/* ------------------------------------------------------------------ */

/* An emulated (QEMU) device has no reachable artifact origin: the OTA command carries a
   Supabase signed URL that the guest's SLIRP-only network can't fetch (there is no guestfwd
   for it, and a raw TCP forward can't serve a hostname/SNI/path-based signed URL). Rather than
   stall the download until the OTA plane reaps the job to timed_out — which leaves the device
   forever on baseline firmware while the deployment falsely reads "finished" — such a device
   adopts the requested version directly, exactly like the L1 software mock (device.py
   _apply_pending_ota). The server-side compile that produced the image is still real; only the
   byte transfer is simulated. Gated on the QEMU device_type so real silicon always does a real
   HTTPS download + partition flip. */
static bool nff_ota_download_simulated(void) {
    const char *dt = g_nff.cfg->device_type;
    return dt != NULL && strcmp(dt, "nff-qemu-esp32") == 0;
}

void nff_ota_handle_cmd(const char *payload, char *resp, size_t resp_len) {
    char version[32]  = {0};
    char url[512]     = {0};
    char sha256hex[65] = {0};
    uint32_t size     = 0;

    /* Refuse a new OTA while a previous image is still on probation — the trial
       must commit or roll back first, otherwise we'd lose the rollback target. */
    if (g_nff.ota_trial) {
        snprintf(resp, resp_len,
                 "{\"type\":\"error\",\"msg\":\"ota: busy (trial in progress)\"}");
        return;
    }

    if (nff_json_get_str(payload, strlen(payload), "version", version, sizeof(version)) != 0 ||
        nff_json_get_str(payload, strlen(payload), "url",     url,     sizeof(url))     != 0 ||
        nff_json_get_str(payload, strlen(payload), "sha256",  sha256hex, sizeof(sha256hex)) != 0) {
        snprintf(resp, resp_len, "{\"type\":\"error\",\"msg\":\"ota: missing fields\"}");
        return;
    }
    nff_json_get_u32(payload, strlen(payload), "size", &size);

    /* Anti-downgrade check */
    const char *fw = g_nff.cfg->fw_version ? g_nff.cfg->fw_version : "0.0.0";
    if (!version_le(fw, version)) {
        snprintf(resp, resp_len,
                 "{\"type\":\"error\",\"msg\":\"ota: downgrade rejected\","
                 "\"current\":\"%s\",\"requested\":\"%s\"}", fw, version);
        return;
    }

    /* Acknowledge receipt before download (download may take minutes) */
    char topic[NFF_TOPIC_MAXLEN];
    nff_topic_response(&g_nff, topic);
    snprintf(resp, resp_len,
             "{\"type\":\"ota_started\",\"version\":\"%s\",\"id\":\"%s\"}",
             version, g_nff.cfg->device_id);
    nff_port_mqtt_publish(g_nff.mqtt, topic, resp, 1, false);

    /* Mark state — nff_loop won't drive heartbeat during OTA */
    g_nff.state = NFF_STATE_OTA_ACTIVE;
    nff_log("nff: OTA start v%s", version);

    /* Emulated device: adopt the version in place instead of a real download + partition flip.
       Persist it (nvs_creds.c reads "fw_adopted" on future boots), point the live config at it so
       THIS session's heartbeat reports the new version immediately, and publish committed so the
       OTA plane finalizes success (advancing current_firmware_version) rather than reaping the job
       to timed_out. No image changes and no reboot — only the version telemetry advances, which is
       exactly what makes the self-heal demo's recovery real and observable. The cfg object is owned
       by the caller of nff_init (non-const storage; the SDK holds a const view), so the cast below
       is well-defined; pending_ota_version is process-lifetime storage in g_nff. */
    if (nff_ota_download_simulated()) {
        strncpy(g_nff.pending_ota_version, version, sizeof(g_nff.pending_ota_version) - 1);
        g_nff.pending_ota_version[sizeof(g_nff.pending_ota_version) - 1] = '\0';
        nff_port_nvs_set_str("fw_adopted", g_nff.pending_ota_version);
        nff_port_nvs_commit();
        ((nff_config_t *)g_nff.cfg)->fw_version = g_nff.pending_ota_version;

        char done[256];
        snprintf(done, sizeof(done),
                 "{\"type\":\"ota_result\",\"status\":\"committed\","
                 "\"version\":\"%s\",\"id\":\"%s\"}",
                 g_nff.pending_ota_version, g_nff.cfg->device_id);
        nff_port_mqtt_publish(g_nff.mqtt, topic, done, 1, false);

        g_nff.state = NFF_STATE_CONNECTED;
        nff_log("nff: OTA simulated-adopt v%s (qemu: no reachable artifact origin)",
                g_nff.pending_ota_version);
        resp[0] = '\0';
        return;
    }

    /* Convert expected SHA-256 hex to bytes */
    uint8_t expected_sha[32] = {0};
    for (int i = 0; i < 32; i++) {
        uint8_t hi, lo;
        char c = sha256hex[i * 2];
        hi = (uint8_t)((c>='a')? c-'a'+10 : (c>='A')? c-'A'+10 : c-'0');
        c  = sha256hex[i * 2 + 1];
        lo = (uint8_t)((c>='a')? c-'a'+10 : (c>='A')? c-'A'+10 : c-'0');
        expected_sha[i] = (uint8_t)((hi << 4) | lo);
    }

    /* Begin OTA partition write */
    nff_ota_handle_t h = nff_port_ota_begin((size_t)size);
    if (h == NFF_OTA_INVALID_HANDLE) {
        snprintf(resp, resp_len, "{\"type\":\"error\",\"msg\":\"ota: begin failed\"}");
        g_nff.state = NFF_STATE_CONNECTED;
        return;
    }

    ota_dl_ctx_t ctx;
    ctx.ota         = h;
    ctx.sha         = nff_port_sha256_new();
    ctx.written     = 0;
    ctx.error       = 0;
    ctx.total       = (size_t)size;         /* 0 = size unknown (optional field) */
    ctx.last_pct    = 0;
    ctx.last_pub_ms = nff_port_millis();
    ctx.attempt     = 1;
    ctx.version     = version;
    ctx.topic       = topic;

    /* Resumable download (IOT_BRIDGE.md §3 gap #6): on a transient TLS stall the transfer is
       retried with an HTTP Range header from where it stopped — the OTA partition keeps
       appending and the SHA-256 context stays live in RAM across retries, so a dropped transfer
       does not restart from byte 0. If the server ignored Range (replied 200 to a resume
       request) the port returns NFF_ERR_RESUME_UNSUPPORTED and we restart the whole download
       clean: re-begin the partition, reset SHA + byte count. In-session only, not cross-reboot. */
    int dl_rc = 0;
    for (int attempt = 0; attempt < NFF_OTA_MAX_DL_RETRIES; attempt++) {
        if (attempt > 0) {
            nff_log("nff: OTA download retry %d (resume @%lu)",
                    attempt, (unsigned long)ctx.written);
            nff_port_delay_ms(NFF_OTA_DL_RETRY_BACKOFF_MS);
        }

        dl_rc = nff_port_https_get_stream(url, ctx.written, ota_chunk_cb, &ctx,
                                          300000 /* 5 min */);

        if (ctx.error != 0) break;                 /* flash write error — not recoverable */
        if (dl_rc == 0 && (size == 0 || ctx.written >= size)) break;  /* complete */

        if (dl_rc == NFF_ERR_RESUME_UNSUPPORTED) {
            /* Server can't resume: discard partial progress and start over from scratch. */
            nff_port_ota_abort(h);
            nff_port_sha256_free(ctx.sha);
            h = nff_port_ota_begin((size_t)size);
            if (h == NFF_OTA_INVALID_HANDLE) { ctx.error = NFF_ERR_OTA; break; }
            ctx.ota     = h;
            ctx.sha     = nff_port_sha256_new();
            ctx.written = 0;
            ctx.error   = 0;
            /* Progress legitimately regresses to 0 here; the attempt bump is what
               authorizes the decrease server-side. last_pct sinks below zero so
               the restarted transfer's first tick publishes immediately. */
            ctx.attempt += 1;
            ctx.last_pct = -100;
            dl_rc       = -1;   /* not done yet; loop continues */
        }
        /* else: partial transfer (stall/short read) — loop and resume from ctx.written */
    }

    /* Finalise SHA-256 */
    uint8_t actual_sha[32] = {0};
    nff_port_sha256_finish(ctx.sha, actual_sha);
    nff_port_sha256_free(ctx.sha);

    bool incomplete = (size != 0 && ctx.written != size);
    if (dl_rc != 0 || ctx.error != 0 || incomplete) {
        nff_port_ota_abort(h);
        nff_log("nff: OTA download failed rc=%d written=%lu/%lu",
                ctx.error ? ctx.error : dl_rc,
                (unsigned long)ctx.written, (unsigned long)size);
        snprintf(resp, resp_len, "{\"type\":\"error\",\"msg\":\"ota: download failed\"}");
        /* Publish the failure — the ota_started ack was the last thing on the wire,
           so without this the cloud would only learn of the failure via the reaper. */
        nff_port_mqtt_publish(g_nff.mqtt, topic, resp, 1, false);
        g_nff.state = NFF_STATE_CONNECTED;
        resp[0] = '\0';
        return;
    }

    /* SHA-256 mismatch → abort */
    if (memcmp(actual_sha, expected_sha, 32) != 0) {
        nff_port_ota_abort(h);
        nff_log("nff: OTA SHA-256 mismatch");
        snprintf(resp, resp_len, "{\"type\":\"error\",\"msg\":\"ota: sha256 mismatch\"}");
        nff_port_mqtt_publish(g_nff.mqtt, topic, resp, 1, false);
        resp[0] = '\0';
        g_nff.state = NFF_STATE_CONNECTED;
        return;
    }

    /* Image verified — surface the install step (partition finalise) to the cloud. */
    ota_publish_progress(topic, version, "installing", 100,
                         ctx.written, ctx.total, ctx.attempt);

    /* Finalise partition — this marks the new partition as bootable */
    if (nff_port_ota_end(h) != 0) {
        nff_log("nff: OTA end failed");
        snprintf(resp, resp_len, "{\"type\":\"error\",\"msg\":\"ota: end failed\"}");
        nff_port_mqtt_publish(g_nff.mqtt, topic, resp, 1, false);
        resp[0] = '\0';
        g_nff.state = NFF_STATE_CONNECTED;
        return;
    }

    /* Put the new image on probation instead of committing optimistically. The
       trial record (no ota_pending yet) tells the next boot to run the health
       gate; commit or rollback decides the final ota_result. See nff_ota_boot_check. */
    nff_port_nvs_set_str("ota_trial",      "1");
    nff_port_nvs_set_str("ota_version",    version);
    nff_port_nvs_set_u32("ota_boot_count", 0);
    nff_port_nvs_commit();

    nff_log("nff: OTA download complete, rebooting into trial v%s", version);

    /* Long-lived stage: covers the reboot + the whole trial/health-soak window
       until ota_result lands. The 500 ms flush below carries it out. */
    ota_publish_progress(topic, version, "rebooting", 100,
                         ctx.written, ctx.total, ctx.attempt);

    /* Small delay to let MQTT ACK flush */
    nff_port_delay_ms(500);
    nff_port_reboot();

    /* Never reached */
    resp[0] = '\0';
}

/* Drain a deferred OTA command (queued by nff_cmd.c) on the nff task. While the
   flag is set no writer touches ota_cmd_buf (nff_cmd.c refuses with "busy"), so
   the handler may read it in place. Mirrors nff_cmd_dispatch's publish-if-nonempty. */
void nff_ota_pending_tick(void) {
    if (!g_nff.ota_cmd_pending) return;

    static char resp[NFF_RESPONSE_MAXLEN];
    resp[0] = '\0';
    nff_ota_handle_cmd(g_nff.ota_cmd_buf, resp, sizeof(resp));

    nff_port_mutex_lock(g_nff.lock);
    g_nff.ota_cmd_pending = false;
    nff_port_mutex_unlock(g_nff.lock);

    if (resp[0] != '\0') {
        char topic[NFF_TOPIC_MAXLEN];
        nff_topic_response(&g_nff, topic);
        nff_port_mqtt_publish(g_nff.mqtt, topic, resp, 1, false);
    }
}
