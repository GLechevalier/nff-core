/*
 * nff power meter — STM32 Nucleo-F446RE
 *
 * A low-side shunt ammeter that accumulates charge and energy in firmware, so a
 * host can ask "what did that OTA cost in joules?".
 *
 * Wiring (see README.md — get this wrong and you measure nothing):
 *
 *     Nucleo 5V ─────────────────────────► ESP32 VIN   (its LDO makes 3V3)
 *     PA0 (ADC) ◄──────┬──────────────────  ESP32 GND
 *                      │
 *                   [ 1 Ω ]  shunt
 *                      │
 *     Nucleo GND ──────┴─────────────────
 *
 *     PA1 (ADC) ◄── 1k/1k divider from the Nucleo 5V rail (supply voltage, for V×I)
 *
 * WHY THE HOST DOESN'T INTEGRATE
 *
 * The obvious design — stream samples to the PC and integrate there — silently
 * under-reports energy the moment the host drops a sample, and you cannot tell from
 * the result that it happened. So instead this firmware keeps exact integer sums of
 * every raw ADC conversion at 100 kSps and reports the running totals; the host only
 * ever multiplies them by constants. A missed conversion cannot hide: it sets the ADC
 * overrun flag, which lands in `ovr`, and the host refuses to report joules when
 * `ovr != 0` rather than handing back a plausible wrong number.
 *
 * Reporting raw sums (rather than joules) also means the host can re-derive energy
 * against a *new* calibration constant without re-running the measurement.
 *
 * ENERGY MATH — kept in integers so a long run can't accumulate float drift.
 *
 *   Let  q  = raw ADC counts on the shunt      (PA0)
 *        u  = raw ADC counts on the divider    (PA1)
 *        L  = VDDA / 4096                      (volts per count)
 *        K  = divider ratio (2.0 for 1k/1k)
 *        R  = shunt resistance
 *
 *   I      = q·L / R
 *   V_esp  = u·K·L − q·L            (supply rail minus the drop across the shunt)
 *   E      = Σ V_esp · I · dt  =  (L²/R)·dt · Σ (K·u·q − q²)
 *
 * So the firmware only has to accumulate Σq, Σq², Σ(u·q) and n. The host does the rest.
 */

#include <Arduino.h>

// ---------------------------------------------------------------- configuration

// 200 kSps per channel. The budget is tight and worth writing down: the ADC clock is
// PCLK2/4 = 22.5 MHz, and a 12-bit conversion costs (sampling_time + 12) cycles. The two
// ranks are 15+12 = 27 and 56+12 = 68 cycles, so one full scan is 95 cycles = 4.22 us against
// the 5.00 us the 200 kHz trigger allows. ~16% headroom; `ovr` reports it if that is ever wrong.
#define FS_HZ 200000u          // ADC trigger rate, per channel
// DMA half-buffer, in (shunt, supply) pairs. 32 pairs = one ISR every 160 us at 200 kSps.
//
// This is deliberately SMALL, and it is not about throughput. The accumulators only advance when
// the half-complete ISR fires, so the block size is the granularity of every measurement taken
// from them. At 256 pairs that granularity was 1.28 ms — LONGER than the 1 ms window the wiring
// probe reads after releasing a driven pin. The probe was therefore sampling either nothing at
// all or a block straddling the drive/release boundary, and reported "floating" for a shunt that
// was demonstrably wired and reading correctly. 32 pairs puts ~6 blocks inside that window.
//
// The cost is ISR rate: 6.25 kHz x ~5 us = ~3% of the CPU. Irrelevant on a 180 MHz M4.
#define PAIRS_PER_HALF 32u
#define BUF_PAIRS (PAIRS_PER_HALF * 2u)
#define BUF_LEN (BUF_PAIRS * 2u)  // 2 channels per pair
#define REPORT_MS 100u            // frame cadence -> 10 Hz

// Calibration lives on the HOST (~/.nff/config.json) and is pushed down with CAL/KDIV/
// VDDA on connect. One source of truth: the meter itself is stateless across resets.
static uint32_t g_shunt_uohm = 1000000u;  // 1 Ω
static uint32_t g_kdiv_milli = 2000u;     // 1k/1k divider -> 2.000
static uint32_t g_vdda_mv = 3300u;

// Frames are emitted on demand (SNAP), not free-running. If the meter chattered at 10 Hz
// the host could never say exactly when accumulation stopped relative to the child process
// exiting — it would over-attribute up to a frame's worth of idle energy to the command.
// STREAM 1 turns the 10 Hz feed on anyway, for the live `nff power monitor` readout.
static bool g_stream = false;

// ---------------------------------------------------------------- peripherals

static ADC_HandleTypeDef hadc1;
static DMA_HandleTypeDef hdma_adc1;
static TIM_HandleTypeDef htim2;

static uint16_t g_buf[BUF_LEN];

// Accumulators. Written only from the DMA/ADC ISRs, read under a critical section.
// uint64 is deliberate: Σ(u·q) grows by up to 4095² ≈ 1.7e7 per sample, so a
// ten-minute run at 100 kSps reaches ~1e15 — comfortable, but it would overflow a
// uint32 in well under a second.
static volatile uint64_t g_n;
static volatile uint64_t g_sum_q;   // Σ q
static volatile uint64_t g_sum_qq;  // Σ q²
static volatile uint64_t g_sum_uq;  // Σ u·q
static volatile uint64_t g_sum_u;   // Σ u  — not needed for energy, but it lets the host
                                    // check the supply divider is actually wired: it must
                                    // work out to ~5 V. Without it, "divider connected" and
                                    // "divider dangling" are indistinguishable, and a
                                    // dangling one silently scales every joule.
static volatile uint32_t g_qmax;
static volatile uint32_t g_ovr;
static volatile bool g_needs_restart;  // set by the ADC error ISR, serviced in loop()
static uint32_t g_t0_ms;

// ---------------------------------------------------------------- hot path

static inline void accumulate(const uint16_t *p, uint32_t pairs) {
  uint64_t sq = 0, sqq = 0, suq = 0, su = 0;
  uint32_t qmax = 0;
  for (uint32_t k = 0; k < pairs; k++) {
    const uint32_t q = p[2 * k];      // rank 1 — PA0, shunt
    const uint32_t u = p[2 * k + 1];  // rank 2 — PA1, supply divider
    sq += q;
    sqq += (uint64_t)q * q;
    suq += (uint64_t)u * q;
    su += u;
    if (q > qmax) qmax = q;
  }
  g_n += pairs;
  g_sum_q += sq;
  g_sum_qq += sqq;
  g_sum_uq += suq;
  g_sum_u += su;
  if (qmax > g_qmax) g_qmax = qmax;
}

extern "C" void HAL_ADC_ConvHalfCpltCallback(ADC_HandleTypeDef *) {
  accumulate(&g_buf[0], PAIRS_PER_HALF);
}

extern "C" void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *) {
  accumulate(&g_buf[BUF_LEN / 2], PAIRS_PER_HALF);
}

extern "C" void HAL_ADC_ErrorCallback(ADC_HandleTypeDef *h) {
  // Overrun: a conversion completed before DMA read the previous one, so samples were lost and
  // the accumulators are now an under-count. Record it — the host sees ovr != 0 and refuses to
  // report an energy figure.
  //
  // DO NOT restart the DMA from in here. HAL_ADC_Stop_DMA() polls HAL_GetTick() waiting for the
  // abort to complete, and HAL_GetTick() is driven by SysTick — which is a LOWER priority than
  // this interrupt. Inside this ISR the tick can never advance, so that poll spins forever and
  // the meter DEADLOCKS: it goes permanently silent, and cannot even report the overrun that
  // killed it. (Observed on the bench: answered for two seconds, then died, with ovr still 0.)
  // Hand the restart to the main loop, where blocking is harmless.
  g_ovr++;
  __HAL_ADC_CLEAR_FLAG(h, ADC_FLAG_OVR);
  g_needs_restart = true;
}

extern "C" void DMA2_Stream0_IRQHandler(void) { HAL_DMA_IRQHandler(&hdma_adc1); }
extern "C" void ADC_IRQHandler(void) { HAL_ADC_IRQHandler(&hadc1); }

// ---------------------------------------------------------------- bring-up

static void meter_start(void) {
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_ADC1_CLK_ENABLE();
  __HAL_RCC_DMA2_CLK_ENABLE();
  __HAL_RCC_TIM2_CLK_ENABLE();

  GPIO_InitTypeDef gpio = {};
  gpio.Pin = GPIO_PIN_0 | GPIO_PIN_1;
  gpio.Mode = GPIO_MODE_ANALOG;
  gpio.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOA, &gpio);

  // TIM2 update -> TRGO, the ADC's sample clock. When the APB1 prescaler is > 1 (it is:
  // F446 runs APB1 at 45 MHz off a 180 MHz core), the timer clock is 2× PCLK1.
  const uint32_t pclk1 = HAL_RCC_GetPCLK1Freq();
  const uint32_t tim_hz = (pclk1 == HAL_RCC_GetHCLKFreq()) ? pclk1 : pclk1 * 2u;
  htim2.Instance = TIM2;
  htim2.Init.Prescaler = 0;
  htim2.Init.CounterMode = TIM_COUNTERMODE_UP;
  htim2.Init.Period = (tim_hz / FS_HZ) - 1u;
  htim2.Init.ClockDivision = TIM_CLOCKDIVISION_DIV1;
  htim2.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_DISABLE;
  HAL_TIM_Base_Init(&htim2);

  TIM_MasterConfigTypeDef master = {};
  master.MasterOutputTrigger = TIM_TRGO_UPDATE;
  master.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  HAL_TIMEx_MasterConfigSynchronization(&htim2, &master);

  // DMA2 Stream0 Channel0 is ADC1's.
  hdma_adc1.Instance = DMA2_Stream0;
  hdma_adc1.Init.Channel = DMA_CHANNEL_0;
  hdma_adc1.Init.Direction = DMA_PERIPH_TO_MEMORY;
  hdma_adc1.Init.PeriphInc = DMA_PINC_DISABLE;
  hdma_adc1.Init.MemInc = DMA_MINC_ENABLE;
  hdma_adc1.Init.PeriphDataAlignment = DMA_PDATAALIGN_HALFWORD;
  hdma_adc1.Init.MemDataAlignment = DMA_MDATAALIGN_HALFWORD;
  hdma_adc1.Init.Mode = DMA_CIRCULAR;
  hdma_adc1.Init.Priority = DMA_PRIORITY_HIGH;
  hdma_adc1.Init.FIFOMode = DMA_FIFOMODE_DISABLE;
  HAL_DMA_Init(&hdma_adc1);
  __HAL_LINKDMA(&hadc1, DMA_Handle, hdma_adc1);

  hadc1.Instance = ADC1;
  hadc1.Init.ClockPrescaler = ADC_CLOCK_SYNC_PCLK_DIV4;  // 90 MHz / 4 = 22.5 MHz (max 36)
  hadc1.Init.Resolution = ADC_RESOLUTION_12B;
  hadc1.Init.ScanConvMode = ENABLE;
  hadc1.Init.ContinuousConvMode = DISABLE;  // the timer paces us, not free-running
  hadc1.Init.DiscontinuousConvMode = DISABLE;
  hadc1.Init.ExternalTrigConv = ADC_EXTERNALTRIGCONV_T2_TRGO;
  hadc1.Init.ExternalTrigConvEdge = ADC_EXTERNALTRIGCONVEDGE_RISING;
  hadc1.Init.DataAlign = ADC_DATAALIGN_RIGHT;
  hadc1.Init.NbrOfConversion = 2;
  hadc1.Init.DMAContinuousRequests = ENABLE;
  hadc1.Init.EOCSelection = ADC_EOC_SINGLE_CONV;
  HAL_ADC_Init(&hadc1);

  ADC_ChannelConfTypeDef ch = {};
  // The shunt node is a ~1 Ω source, so it settles fast — 15 cycles is plenty.
  ch.Channel = ADC_CHANNEL_0;
  ch.Rank = 1;
  ch.SamplingTime = ADC_SAMPLETIME_15CYCLES;
  HAL_ADC_ConfigChannel(&hadc1, &ch);
  // 56 cycles = 2.49 us at a 22.5 MHz ADC clock. That covers either divider the rig allows:
  // a 10k/10k pair is a 5 kOhm source, so the 4 pF sample-and-hold (charging through that plus
  // ~6 kOhm of internal mux resistance) has tau ~= 44 ns and needs ~9 tau ~= 0.4 us to settle to
  // 12 bits — about 6x margin. 1k/1k is easier still. Don't cut this below 56 without redoing
  // that sum: an under-sampled high-impedance source reads LOW, silently, and every joule
  // scales with the rail.
  ch.Channel = ADC_CHANNEL_1;
  ch.Rank = 2;
  ch.SamplingTime = ADC_SAMPLETIME_56CYCLES;
  HAL_ADC_ConfigChannel(&hadc1, &ch);

  // Priority 5, NOT 1 — deliberately BELOW the UART.
  //
  // accumulate() chews through 256 samples and takes ~43 us. STM32duino runs the USART IRQ at
  // priority 1, so if we sit at 1 as well there is no preemption and the UART cannot be serviced
  // for that whole stretch. At 921600 baud a byte lands every 10.8 us, so FOUR received bytes get
  // overwritten in the data register on every ISR — commands from the host arrive corrupted.
  //
  // That is not theoretical: it produced `unknown command: "SNAPPING"` (a dropped newline glued
  // SNAP and PING together), meters that went "silent" mid-run, and almost certainly the lost
  // ZERO that once made a 30 s run report 91 s. Sitting below the UART lets it preempt us. The
  // DMA has a 1.28 ms cushion before it laps the half-buffer we are still reading, so being
  // interrupted by a few microseconds of UART work is completely safe.
  HAL_NVIC_SetPriority(DMA2_Stream0_IRQn, 5, 0);
  HAL_NVIC_EnableIRQ(DMA2_Stream0_IRQn);
  HAL_NVIC_SetPriority(ADC_IRQn, 5, 0);
  HAL_NVIC_EnableIRQ(ADC_IRQn);

  HAL_ADC_Start_DMA(&hadc1, (uint32_t *)(void *)g_buf, BUF_LEN);
  HAL_TIM_Base_Start(&htim2);
}

static void meter_zero(void) {
  noInterrupts();
  g_n = 0;
  g_sum_q = 0;
  g_sum_qq = 0;
  g_sum_uq = 0;
  g_sum_u = 0;
  g_qmax = 0;
  g_ovr = 0;
  interrupts();
  g_t0_ms = millis();
}

// ---------------------------------------------------------------- continuity probe

/*
 * Is a pin actually connected to anything?
 *
 * You cannot tell from the reading alone. A floating ADC pin does not read zero — it holds
 * charge on its sample-and-hold cap, and in bring-up an unwired divider input sat at a wholly
 * convincing "4.94 V" while an unwired shunt input reported 590 mA. Passive plausibility
 * checks are defeated by luck.
 *
 * The internal pull-ups are no help either: they are disabled in ANALOG mode, and the ADC
 * cannot read the pad in digital-INPUT mode (the analog switch is open), so a pull-based probe
 * measures nothing — it reported both pins "connected" with nothing attached at all.
 *
 * So: briefly DRIVE the node, release it back to analog, and read it immediately. A real
 * source restores its own voltage in nanoseconds (the divider is a ~500 Ω source; the shunt
 * node is a near short to ground). A floating node has nothing to restore it and stays where
 * it was driven, for milliseconds.
 *
 * SAFETY: PA0 is only ever driven LOW. Driving it HIGH would push the GPIO against the 1 Ω
 * shunt straight to ground — roughly 65 mA, well past the pin's 25 mA rating. PA1 is safe to
 * drive both ways because the 1k/1k divider limits the current to a few mA.
 */
static uint32_t mean_over(uint32_t ms, bool shunt_channel) {
  noInterrupts();
  const uint64_t n0 = g_n;
  const uint64_t s0 = shunt_channel ? g_sum_q : g_sum_u;
  interrupts();

  HAL_Delay(ms);

  noInterrupts();
  const uint64_t n1 = g_n;
  const uint64_t s1 = shunt_channel ? g_sum_q : g_sum_u;
  interrupts();

  return (n1 > n0) ? (uint32_t)((s1 - s0) / (n1 - n0)) : 0;
}

static void set_analog(uint16_t pin) {
  GPIO_InitTypeDef g = {};
  g.Pin = pin;
  g.Mode = GPIO_MODE_ANALOG;
  g.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOA, &g);
}

/* Drive the node to `level`, release to analog, and report what it reads right after. */
static uint32_t probe_release(uint16_t pin, GPIO_PinState level, bool shunt_channel) {
  GPIO_InitTypeDef g = {};
  g.Pin = pin;
  g.Mode = GPIO_MODE_OUTPUT_PP;
  g.Pull = GPIO_NOPULL;
  g.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOA, &g);
  HAL_GPIO_WritePin(GPIOA, pin, level);
  HAL_Delay(2);

  set_analog(pin);
  return mean_over(1, shunt_channel);  // read IMMEDIATELY — a real source has already won
}

static void emit_wirecheck(void) {
  const uint32_t q_base = mean_over(2, true);
  const uint32_t q_lo = probe_release(GPIO_PIN_0, GPIO_PIN_RESET, true);

  const uint32_t u_base = mean_over(2, false);
  const uint32_t u_lo = probe_release(GPIO_PIN_1, GPIO_PIN_RESET, false);
  const uint32_t u_hi = probe_release(GPIO_PIN_1, GPIO_PIN_SET, false);

  Serial.print("{\"wirecheck\":true,\"q_base\":");
  Serial.print(q_base);
  Serial.print(",\"q_lo\":");
  Serial.print(q_lo);
  Serial.print(",\"u_base\":");
  Serial.print(u_base);
  Serial.print(",\"u_lo\":");
  Serial.print(u_lo);
  Serial.print(",\"u_hi\":");
  Serial.print(u_hi);
  Serial.println("}");
}

// ---------------------------------------------------------------- host protocol

// Arduino's Serial.print() has no uint64_t overload, and newlib-nano's printf drops
// %llu unless you relink it. Neither is worth the trouble for six numbers.
static void print_u64(uint64_t v) {
  char b[21];
  int i = 20;
  b[i] = '\0';
  if (v == 0) b[--i] = '0';
  while (v) {
    b[--i] = (char)('0' + (v % 10u));
    v /= 10u;
  }
  Serial.print(&b[i]);
}

static void emit_frame(void) {
  uint64_t n, sq, sqq, suq, su;
  uint32_t qmax, ovr;
  noInterrupts();
  n = g_n;
  sq = g_sum_q;
  sqq = g_sum_qq;
  suq = g_sum_uq;
  su = g_sum_u;
  qmax = g_qmax;
  ovr = g_ovr;
  interrupts();

  Serial.print("{\"t\":");
  Serial.print(millis() - g_t0_ms);
  Serial.print(",\"n\":");
  print_u64(n);
  Serial.print(",\"ovr\":");
  Serial.print(ovr);
  Serial.print(",\"sq\":");
  print_u64(sq);
  Serial.print(",\"sqq\":");
  print_u64(sqq);
  Serial.print(",\"suq\":");
  print_u64(suq);
  Serial.print(",\"su\":");
  print_u64(su);
  Serial.print(",\"qmax\":");
  Serial.print(qmax);
  Serial.print(",\"fs\":");
  Serial.print(FS_HZ);
  Serial.print(",\"vdda\":");
  Serial.print(g_vdda_mv);
  Serial.print(",\"r\":");
  Serial.print(g_shunt_uohm);
  Serial.print(",\"kdiv\":");
  Serial.print(g_kdiv_milli);
  Serial.println("}");
}

static void emit_info(void) {
  Serial.print("{\"meter\":\"nff-power-meter\",\"version\":1,\"board\":\"nucleo_f446re\",\"fs\":");
  Serial.print(FS_HZ);
  Serial.print(",\"vdda\":");
  Serial.print(g_vdda_mv);
  Serial.print(",\"r\":");
  Serial.print(g_shunt_uohm);
  Serial.print(",\"kdiv\":");
  Serial.print(g_kdiv_milli);
  Serial.println("}");
}

static void handle_line(char *line) {
  char *arg = strchr(line, ' ');
  if (arg) *arg++ = '\0';

  if (!strcmp(line, "PING")) {
    Serial.println("{\"ok\":true,\"meter\":\"nff-power-meter\"}");
  } else if (!strcmp(line, "INFO")) {
    emit_info();
  } else if (!strcmp(line, "ZERO")) {
    meter_zero();
    Serial.println("{\"ok\":true,\"zeroed\":true}");
  } else if (!strcmp(line, "SNAP")) {
    emit_frame();
  } else if (!strcmp(line, "WIRECHECK")) {
    emit_wirecheck();
  } else if (!strcmp(line, "STREAM") && arg) {
    g_stream = strtoul(arg, nullptr, 10) != 0;
    Serial.print("{\"ok\":true,\"stream\":");
    Serial.print(g_stream ? 1 : 0);
    Serial.println("}");
  } else if (!strcmp(line, "CAL") && arg) {
    g_shunt_uohm = (uint32_t)strtoul(arg, nullptr, 10);
    emit_info();
  } else if (!strcmp(line, "KDIV") && arg) {
    g_kdiv_milli = (uint32_t)strtoul(arg, nullptr, 10);
    emit_info();
  } else if (!strcmp(line, "VDDA") && arg) {
    g_vdda_mv = (uint32_t)strtoul(arg, nullptr, 10);
    emit_info();
  } else {
    Serial.print("{\"ok\":false,\"error\":\"unknown command: ");
    Serial.print(line);
    Serial.println("\"}");
  }
}

static void poll_commands(void) {
  static char buf[48];
  static uint8_t len = 0;
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (len) {
        buf[len] = '\0';
        handle_line(buf);
        len = 0;
      }
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}

// ---------------------------------------------------------------- arduino entry

void setup() {
  Serial.begin(921600);
  meter_start();
  meter_zero();
  emit_info();
}

void loop() {
  static uint32_t next = 0;

  // Service an overrun reported by the ADC error ISR. Doing this here rather than in the ISR is
  // the whole point — see HAL_ADC_ErrorCallback. The lost samples are already counted in g_ovr,
  // so the host still knows the run is untrustworthy; we just keep the meter ALIVE to say so.
  if (g_needs_restart) {
    g_needs_restart = false;
    HAL_ADC_Stop_DMA(&hadc1);
    HAL_ADC_Start_DMA(&hadc1, (uint32_t *)(void *)g_buf, BUF_LEN);
  }

  poll_commands();
  if (g_stream && (int32_t)(millis() - next) >= 0) {
    next = millis() + REPORT_MS;
    emit_frame();
  }
}
