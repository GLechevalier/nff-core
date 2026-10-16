/**
 * arduino_onboard_wired.ino — WIRED-FIRST dual-mode golden onboarding image.
 *
 * Boot decides the transport from stored credentials (see nff_wifi_creds.h):
 *   1. WiFi creds in NVS            -> WiFi mode (board runs standalone, over the air)
 *   2. WiFi creds patched in slots  -> WiFi mode, persisting the creds to NVS on first connect
 *   3. none                         -> PPPoS over USB (UART0), zero-config demo mode
 *
 * PPPoS mode: the device brings up a PPPoS netif on UART0 and the (netif-agnostic) nff SDK runs
 * its mTLS/MQTT over it. The host end is the DASHBOARD BROWSER TAB (src/lib/ppplink/ WiredLink):
 * it answers DNS+NTP locally and splices the single TCP flow to the fleet broker over a WebSocket
 * bridge — no WiFi, no install, no admin.
 *
 * Cut-the-cord handoff (stage 2 of onboarding): the dashboard tears the link down with an LCP
 * Terminate-Req, which drops PPP into a 60 s config window where UART0 is a plain line channel:
 *     dashboard -> "NFF+CFG <base64 ssid> <base64 pass|->\n"
 *     device    -> "NFF+OK\n"  (creds stored in NVS), then reboot into WiFi mode
 * If no config line arrives (e.g. the tab just closed), the device re-raises PPP and keeps
 * retrying forever, so replugging/reopening the tab re-attaches it.
 *
 * CONSTRAINTS (PPPoS mode):
 *  - PPP owns UART0, so nothing may print to Serial while PPP is up — any log byte would corrupt
 *    PPP frames. Online-ness is observed from the FLEET, not serial. (The config window and WiFi
 *    mode may print: PPP is dead there.)
 *  - esp_netif_init() MUST run before any pppapi_* call, or lwIP never transmits LCP (hard gotcha).
 *  - A dedicated RX task pumps UART0 -> pppos_input_tcpip continuously, so nff_bootstrap_run() can
 *    block while the PPP link keeps receiving the rollover cert.
 *
 * NFF_BOOTSTRAP_ENABLED=1 is set via build_opt.h (reaches the library TUs, e.g. nff_claim.c).
 */

#include <WiFi.h>
#include <nff.h>
#include "credentials.h"     // magic patch slots + nff_onboard_fill_config (shared with the WiFi image)
#include "nff_wifi_creds.h"  // NVS-persisted WiFi creds (NVS -> slots -> wired resolution)

extern "C" {
  #include "netif/ppp/pppapi.h"
  #include "netif/ppp/pppos.h"
  #include "lwip/dns.h"
  #include "lwip/ip_addr.h"
  #include "esp_netif.h"
  #include "esp_event.h"
  #include "mbedtls/base64.h"
}

// Baud for the PPP link over USB — must match the browser WiredLink baud. 460800 is the
// fast-but-safe default (921600 corrupts on some USB-serial bridges).
static const uint32_t PPP_BAUD = 460800;

// Config window after a link drop before PPP is re-raised.
static const uint32_t CFG_WINDOW_MS = 60000;

static nff_config_t   g_cfg;
static ppp_pcb       *s_ppp;
static struct netif   s_ppp_netif;
static volatile bool  s_ppp_up   = false;
static volatile bool  s_ppp_dead = false;  // status_cb reported a non-NONE error (link down/never up)
static volatile bool  s_cfg_window = false; // rx pump paused; supervisor owns UART0 as a line channel

/* ------------------------------------------------------------------ */
/* WiFi mode (NVS or slot creds)                                       */
/* ------------------------------------------------------------------ */

// Mirrors arduino_onboard.ino. `persist` stores slot-sourced creds to NVS after a successful
// connect so future flashes (e.g. Code-tab repo builds) never need WiFi creds again.
static void run_wifi_mode(const char *ssid, const char *pass, bool persist) {
    Serial.begin(115200);
    nff_onboard_fill_config(&g_cfg);

    Serial.printf("nff onboard: connecting to WiFi '%s'...\n", ssid);
    WiFi.begin(ssid, pass);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 30000) {
        delay(250);
        Serial.print(".");
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\nWiFi failed - restarting in 5s");
        delay(5000);
        ESP.restart();
    }
    Serial.printf("\nWiFi connected, IP: %s -> broker %s:8883\n",
                  WiFi.localIP().toString().c_str(), g_cfg.broker_host);
    if (persist) nff_wifi_creds_store(ssid, pass);

    if (nff_init(&g_cfg) != NFF_OK) {
        Serial.println("nff_init failed");
        return;
    }
    if (nff_get_mode() == NFF_MODE_BOOTSTRAP) {
        Serial.println("nff: BOOTSTRAP mode - announcing for enrollment");
        nff_bootstrap_run();   // connect on shared cred, announce, await rollover, reboot CLAIMED
    } else {
        Serial.println("nff: CLAIMED mode - connecting operationally");
        nff_connect();
    }
}

/* ------------------------------------------------------------------ */
/* PPPoS (wired) mode                                                  */
/* ------------------------------------------------------------------ */

// lwIP hands us PPP bytes to transmit on the serial line.
static u32_t ppp_output_cb(ppp_pcb *pcb, const void *data, u32_t len, void *ctx) {
    (void)pcb; (void)ctx;
    return Serial.write((const uint8_t *)data, len);
}

// PPPERR_NONE = IPCP up (we have an IP + default route). Anything else = link down — either the
// dashboard sent LCP Terminate-Req (cut-the-cord) or the tab closed; the supervisor task reacts.
static void ppp_status_cb(ppp_pcb *pcb, int err_code, void *ctx) {
    (void)pcb; (void)ctx;
    if (err_code == PPPERR_NONE) { s_ppp_up = true;  s_ppp_dead = false; }
    else                         { s_ppp_up = false; s_ppp_dead = true;  }
}

// Continuously feed inbound serial bytes into the PPP input path — unless the supervisor has
// claimed UART0 for a config window.
static void ppp_rx_task(void *arg) {
    (void)arg;
    uint8_t buf[512];
    for (;;) {
        if (s_cfg_window) { vTaskDelay(10); continue; }
        int n = 0;
        while (Serial.available() && n < (int)sizeof(buf)) buf[n++] = (uint8_t)Serial.read();
        if (n > 0) pppos_input_tcpip(s_ppp, buf, n);
        else vTaskDelay(1);
    }
}

// Decode one base64 field ("-" means empty). Returns false on decode/size failure.
static bool decode_b64_field(const char *b64, size_t b64_len, char *out, size_t out_cap) {
    if (b64_len == 1 && b64[0] == '-') { out[0] = '\0'; return true; }
    size_t olen = 0;
    if (mbedtls_base64_decode((unsigned char *)out, out_cap - 1, &olen,
                              (const unsigned char *)b64, b64_len) != 0) return false;
    out[olen] = '\0';
    return true;
}

// Parse "NFF+CFG <b64 ssid> <b64 pass|->". On success stores creds in NVS, acks, reboots.
static bool handle_cfg_line(char *line) {
    if (strncmp(line, "NFF+CFG ", 8) != 0) return false;
    char *ssid_b64 = line + 8;
    char *sp = strchr(ssid_b64, ' ');
    if (!sp) { Serial.print("NFF+ERR parse\n"); return true; }
    *sp = '\0';
    char *pass_b64 = sp + 1;

    char ssid[NFF_WIFI_SSID_MAX], pass[NFF_WIFI_PASS_MAX];
    if (!decode_b64_field(ssid_b64, strlen(ssid_b64), ssid, sizeof(ssid)) || ssid[0] == '\0' ||
        !decode_b64_field(pass_b64, strlen(pass_b64), pass, sizeof(pass))) {
        Serial.print("NFF+ERR parse\n");
        return true;
    }
    nff_wifi_creds_store(ssid, pass);
    Serial.print("NFF+OK\n");
    Serial.flush();
    delay(200);
    ESP.restart();
    return true; // unreachable
}

// Owns link lifecycle: (re)raises PPP whenever it is dead, and on each drop opens a config window
// in which UART0 is a plain line channel for the cut-the-cord handoff.
static void ppp_supervisor_task(void *arg) {
    (void)arg;
    for (;;) {
        if (!s_ppp_dead) { vTaskDelay(200); continue; }

        // Link dropped (or never came up): give the dashboard a config window.
        s_cfg_window = true;
        char line[256];
        size_t pos = 0;
        uint32_t t0 = millis();
        while (millis() - t0 < CFG_WINDOW_MS) {
            while (Serial.available()) {
                char c = (char)Serial.read();
                if (c == '\n' || c == '\r') {
                    if (pos > 0) {
                        line[pos] = '\0';
                        pos = 0;
                        if (handle_cfg_line(line)) { t0 = millis(); }  // acked (or NFF+ERR): keep window
                    }
                } else if (pos < sizeof(line) - 1) {
                    line[pos++] = c;
                } else {
                    pos = 0;  // over-long garbage (e.g. stray PPP bytes): resync on next newline
                }
            }
            vTaskDelay(10);
        }

        // No handoff — re-raise PPP and keep serving the wired link (tab may reopen anytime).
        s_cfg_window = false;
        s_ppp_dead = false;
        pppapi_connect(s_ppp, 0);
    }
}

static void run_wired_mode(void) {
    Serial.begin(PPP_BAUD);
    Serial.setRxBufferSize(2048);   // avoid RX overflow at high baud before the pump task drains it
    // From here on UART0 is the PPP link — do NOT print to Serial (except inside a config window).

    // The lwIP tcpip thread must exist before pppapi_* (which marshal to it), else LCP never sends.
    esp_netif_init();
    esp_event_loop_create_default();

    s_ppp = pppapi_pppos_create(&s_ppp_netif, ppp_output_cb, ppp_status_cb, NULL);
    if (!s_ppp) return;
    pppapi_set_default(s_ppp);
    pppapi_connect(s_ppp, 0);

    xTaskCreatePinnedToCore(ppp_rx_task, "ppp_rx", 4096, NULL, 12, NULL, 0);
    xTaskCreatePinnedToCore(ppp_supervisor_task, "ppp_sup", 4096, NULL, 5, NULL, 0);

    // Wait (bounded) for the browser WiredLink to negotiate LCP/IPCP; the supervisor keeps
    // retrying beyond this if the tab isn't there yet.
    uint32_t t0 = millis();
    while (!s_ppp_up && (millis() - t0) < 60000) delay(50);
    if (!s_ppp_up) return;   // supervisor keeps the link retrying; SDK just doesn't start yet

    // Point DNS at the WiredLink router (10.0.0.1): it answers DNS + NTP locally from the browser
    // clock (SNTP must succeed before the mTLS handshake — cert validity needs wall time).
    ip_addr_t dnssrv;
    ipaddr_aton("10.0.0.1", &dnssrv);
    dns_setserver(0, &dnssrv);

    // Enrollment is IDENTICAL to the WiFi image: fill config from the patched slots, then claim.
    nff_onboard_fill_config(&g_cfg);   // broker_host baked; the WiredLink splices the flow to the fleet
    if (nff_init(&g_cfg) != NFF_OK) return;

    if (nff_get_mode() == NFF_MODE_BOOTSTRAP) {
        nff_bootstrap_run();   // announce, await rollover, reboot CLAIMED (does not return on success)
    } else {
        // CLAIMED: nff_connect() actually opens the MQTT session (nff_mqtt_init). nff_start_task
        // alone never dials — nff_mqtt_tick() is a no-op while g_nff.mqtt is NULL, which was the
        // long-standing "device reboots CLAIMED but never comes online" gap on the wired path.
        nff_connect();
        nff_start_task();      // service the session on its own task
    }
}

/* ------------------------------------------------------------------ */

static bool s_wifi_mode = false;

void setup() {
    char ssid[NFF_WIFI_SSID_MAX], pass[NFF_WIFI_PASS_MAX];
    if (nff_wifi_creds_load(ssid, sizeof(ssid), pass, sizeof(pass))) {
        s_wifi_mode = true;
        run_wifi_mode(ssid, pass, /*persist=*/false);      // 1. NVS (cut-the-cord ran)
    } else if (WIFI_SSID[0] != '\0') {
        s_wifi_mode = true;
        run_wifi_mode(WIFI_SSID, WIFI_PASS, /*persist=*/true);  // 2. patched slots (advanced WiFi flash)
    } else {
        run_wired_mode();                                   // 3. zero-config USB demo
    }
}

void loop() {
    if (s_wifi_mode) nff_loop();   // wired mode is serviced by its own tasks
    delay(s_wifi_mode ? 10 : 100);
}
