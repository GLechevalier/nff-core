# Supported Boards

**On the default PlatformIO backend, nff is board-universal.** Pass any of PlatformIO's [~1000+ board ids](https://docs.platformio.org/en/latest/boards/index.html) to `--board` and the matching platform toolchain (compiler + framework + uploader) installs itself on first build — there is no fixed allow-list and nothing to pre-install.

## Build backends

A single setting decides the toolchain for every `compile`/`flash` entry point, CLI and MCP alike:

1. **`NFF_BUILD_BACKEND`** env var — per-run override (`platformio`/`pio` or `arduino`/`arduino-cli`)
2. **`build.backend`** in `~/.nff/config.json` — the persisted choice (written by `nff init --backend …`)
3. **Default → `platformio`**

The classic **arduino-cli backend** remains available and covers ESP32 (CP210x / CH340) · ESP8266 (FTDI) · Arduino AVR (Uno, Mega, Nano, Leonardo), plus whatever cores you `arduino-cli core install`. On that backend the board is named by FQBN (`default_device.fqbn`) rather than a PlatformIO board id.

See [platformio-backend.md](platformio-backend.md) for the design and history of the PlatformIO backend, and [CONFIGURATION.md](CONFIGURATION.md) for the config file schema.

## Architectures & platforms covered

The PlatformIO backend gives nff every PlatformIO **development platform** — each one a whole family of boards. You don't need any of these in nff's catalog; just pass the PlatformIO board id to `--board` and the toolchain installs on first build. The table below is the full set of platforms (≈40), each spanning dozens-to-hundreds of individual boards.

| Platform (`--board` resolves it) | Core / MCU family | Example boards & `--board` ids |
|---|---|---|
| `espressif32` | Espressif ESP32 (Xtensa LX6/LX7 + RISC-V) | ESP32, ESP32-S2, ESP32-S3, ESP32-C3/C6/H2, ESP32-P4 — `esp32dev`, `esp32-s3-devkitc-1`, `esp32-c6-devkitc-1` |
| `espressif8266` | Espressif ESP8266 (Tensilica L106) | NodeMCU, Wemos D1 — `esp01_1m`, `nodemcuv2`, `d1_mini` |
| `raspberrypi` | Raspberry Pi RP2040 / RP2350 (ARM Cortex-M0+/M33) | Pico, Pico W, Pico 2 — `pico`, `rpipicow`, `rpipico2` |
| `atmelavr` | Classic 8-bit Atmel AVR | Arduino Uno/Mega/Nano/Leonardo, Pro Mini — `uno`, `megaatmega2560`, `nanoatmega328`, `leonardo` |
| `atmelmegaavr` | Atmel megaAVR (0-series) | Arduino Uno WiFi Rev2, Nano Every — `uno_wifi_rev2`, `nano_every` |
| `atmelsam` | Atmel SAM (ARM Cortex-M0+/M3/M4) | Arduino Zero/MKR/Due, Adafruit Feather M0/M4 — `mkrwifi1010`, `adafruit_feather_m4`, `due` |
| `ststm32` | ST STM32 (ARM Cortex-M0/0+/M3/M4/M7) — F0/F1/F2/F3/F4/F7/G0/G4/H7/L0/L1/L4/L5/U5/WB/WL | Blue Pill, Black Pill, every Nucleo/Discovery — `bluepill_f103c8`, `genericSTM32F103C8`, `nucleo_f401re`, `nucleo_h743zi` |
| `ststm8` | ST STM8 (8-bit) | STM8S Discovery, sduino — `stm8sdiscovery` |
| `teensy` | PJRC Teensy (ARM Cortex-M4/M7) | Teensy 3.x / 4.0 / 4.1 / LC — `teensy41`, `teensy40`, `teensy36`, `teensylc` |
| `nordicnrf52` | Nordic nRF52 (ARM Cortex-M4, BLE) | Adafruit Feather/ItsyBitsy nRF52840, Nano 33 BLE — `nano33ble`, `adafruit_feather_nrf52840` |
| `nordicnrf51` | Nordic nRF51 (ARM Cortex-M0, BLE) | micro:bit v1, BBC boards — `bbcmicrobit`, `nrf51_dk` |
| `renesas-ra` | Renesas RA4M1 (ARM Cortex-M4) | **Arduino Uno R4** Minima / WiFi — `uno_r4_minima`, `uno_r4_wifi` |
| `nxplpc` | NXP LPC (ARM Cortex-M0/M3/M4) | mbed LPC1768, LPC11U24 — `lpc1768`, `lpc11u35` |
| `nxpimxrt` | NXP i.MX RT (ARM Cortex-M7) | MIMXRT1060/1010 EVK — `mimxrt1060_evk` |
| `freescalekinetis` | NXP/Freescale Kinetis (ARM Cortex-M0+/M4) | FRDM-K64F, FRDM-KL25Z — `frdm_k64f`, `frdm_kl25z` |
| `siliconlabsefm32` | Silicon Labs EFM32 (ARM Cortex-M) | EFM32 Giant/Wonder Gecko — `efm32gg_stk3700` |
| `gd32v` | GigaDevice GD32V (RISC-V) | Sipeed Longan Nano — `sipeed-longan-nano` |
| `kendryte210` | Kendryte K210 (RISC-V, AI) | Sipeed MAIX — `sipeed-maix-bit` |
| `microchippic32` | Microchip PIC32 (MIPS) | chipKIT Uno32, Max32 — `chipkit_uno32`, `chipkit_max32` |
| `timsp430` | TI MSP430 (16-bit) | MSP430 LaunchPads — `lpmsp430g2553`, `lpmsp430fr6989` |
| `titiva` | TI TIVA C (ARM Cortex-M4) | Tiva C / Stellaris LaunchPad — `lptm4c1230c3pm`, `lplm4f120h5qr` |
| `infineonxmc` | Infineon XMC (ARM Cortex-M) | XMC2Go, XMC1100 Boot Kit — `xmc1100_xmc2go` |
| `intel_arc32` | Intel Curie (ARC) | Arduino/Genuino 101 — `genuino101` |
| `wiznet7500` | WIZnet W7500 (ARM Cortex-M0, Ethernet) | WIZwiki-W7500 — `wizwiki_w7500` |
| `lattice_ice40` | Lattice iCE40 FPGA | TinyFPGA B2, iCEstick — `icezum`, `tinyfpga_b2` |

> Less common platforms PlatformIO also ships (and that nff therefore drives) include `nuclei`, `riscv_gap`, `samd21`, `chipsalliance`, `aceinna_imu`, `shakti`, `samsung_artik`, and others — see the [PlatformIO platforms index](https://docs.platformio.org/en/latest/platforms/index.html) for the live, complete list.

## Curated families (built-in catalog)

These are the board ids in nff's built-in catalog. The catalog only supplies sensible defaults (PlatformIO platform) so you can name a short board id and `nff init` can auto-detect — **every other board above still builds**, you just pass the full PlatformIO id.

| Family | PlatformIO platform | Catalogued `--board` ids |
|---|---|---|
| **ESP32** | `espressif32` | `esp32dev`, `esp32-s3-devkitc-1`, `esp32-c3-devkitm-1`, `esp32-c6-devkitc-1`, `esp32-s2-saola-1` |
| **ESP8266** | `espressif8266` | `esp01_1m`, `nodemcuv2` |
| **RP2040 / Pico** | `raspberrypi` | `pico`, `rpipicow` |
| **STM32** | `ststm32` | `genericSTM32F103C8`, `bluepill_f103c8`, `nucleo_f401re` |
| **Classic AVR** | `atmelavr` | `uno`, `megaatmega2560`, `nanoatmega328`, `leonardo` |

Need a board that isn't catalogued (Teensy, SAMD, nRF52, Uno R4, ESP32-P4, …)? Just give its PlatformIO id — e.g. `nff compile sketch.ino --board teensy41`. Adding it to the catalog (for auto-detect + a short default) is a [two-line PR](../CONTRIBUTING.md#adding-a-new-board).

## USB auto-detect

When you plug a board in, nff resolves it by USB vendor/product ID to a default board id for **both** backends, so `nff init` and `--board`-less commands "just work". A USB-serial chip (CP210x/CH340/FTDI) is shared by many boards, so this is a *default* you can override with `--board`.

| Board | Vendor ID | Product ID | FQBN (arduino) | PlatformIO board id |
|---|---|---|---|---|
| ESP32 (CP210x) | 10c4 | ea60 | `esp32:esp32:esp32` | `esp32dev` |
| ESP32 (CH340) | 1a86 | 7523 | `esp32:esp32:esp32` | `esp32dev` |
| ESP8266 (FTDI) | 0403 | 6001 | `esp8266:esp8266:generic` | `esp01_1m` |
| Arduino Uno | 2341 | 0043 | `arduino:avr:uno` | `uno` |
| Arduino Mega 2560 | 2341 | 0010 | `arduino:avr:mega` | `megaatmega2560` |
| Arduino Leonardo | 2341 | 0036 | `arduino:avr:leonardo` | `leonardo` |
| Arduino Nano | 2341 | 0058 | `arduino:avr:nano` | `nanoatmega328` |

> The **arduino backend** (`NFF_BUILD_BACKEND=arduino`) is limited to the FQBN column above plus whatever cores you `arduino-cli core install`. The PlatformIO backend is the one that makes the rest of the families above available.
