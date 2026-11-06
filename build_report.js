const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  ImageRun, Table, TableRow, TableCell, WidthType, BorderStyle, ShadingType,
} = require("docx");

const OUT = "/home/claude/parrot_sim/out";
const img = (f) => fs.readFileSync(`${OUT}/${f}`);

const FONT = "Calibri";
const ACCENT = "1F3864";

function h(text, level) { return new Paragraph({ heading: level, children: [new TextRun({ text })] }); }
function p(runs, opts = {}) {
  const children = (Array.isArray(runs) ? runs : [runs]).map((r) =>
    typeof r === "string" ? new TextRun({ text: r }) : new TextRun(r));
  return new Paragraph({ children, spacing: { after: 140, line: 276 }, ...opts });
}
function bullet(runs) {
  const children = (Array.isArray(runs) ? runs : [runs]).map((r) =>
    typeof r === "string" ? new TextRun({ text: r }) : new TextRun(r));
  return new Paragraph({ children, bullet: { level: 0 }, spacing: { after: 80, line: 276 } });
}
function figure(file, caption, w = 470, hgt) {
  const ratio = file.includes("fig4") ? 702 / 546 : 936 / 546;
  const height = hgt || Math.round(w / ratio);
  return [
    new Paragraph({
      alignment: AlignmentType.CENTER, spacing: { before: 120, after: 40 },
      children: [new ImageRun({ type: "png", data: img(file), transformation: { width: w, height } })],
    }),
    new Paragraph({
      alignment: AlignmentType.CENTER, spacing: { after: 180 },
      children: [new TextRun({ text: caption, italics: true, size: 18, color: "555555" })],
    }),
  ];
}

// ---- results table ---------------------------------------------------------
function cell(text, { bold = false, w = 2000, fill } = {}) {
  return new TableCell({
    width: { size: w, type: WidthType.DXA },
    margins: { top: 40, bottom: 40, left: 80, right: 80 },
    shading: fill ? { type: ShadingType.CLEAR, color: "auto", fill } : undefined,
    children: [new Paragraph({ children: [new TextRun({ text, bold, size: 18 })] })],
  });
}
const COLS = [2300, 1500, 2600, 1600, 1640];
function row(cells, header = false) {
  return new TableRow({
    tableHeader: header,
    children: cells.map((c, i) =>
      cell(c, { bold: header, w: COLS[i], fill: header ? ACCENT : undefined })),
  });
}
// header text white
function headerRow(cells) {
  return new TableRow({
    tableHeader: true,
    children: cells.map((c, i) => new TableCell({
      width: { size: COLS[i], type: WidthType.DXA },
      margins: { top: 40, bottom: 40, left: 80, right: 80 },
      shading: { type: ShadingType.CLEAR, color: "auto", fill: ACCENT },
      children: [new Paragraph({ children: [new TextRun({ text: c, bold: true, size: 18, color: "FFFFFF" })] })],
    })),
  });
}
const resultsTable = new Table({
  columnWidths: COLS,
  width: { size: COLS.reduce((a, b) => a + b, 0), type: WidthType.DXA },
  rows: [
    headerRow(["Scenario", "Defense", "Spoofs reaching the bus", "Attacker", "Defender"]),
    row(["E1 — baseline", "none", "100 (all land)", "healthy", "—"]),
    row(["E2 — Parrot", "Parrot", "0", "bus-off @ ~94 ms", "TEC ≈ 0"]),
    row(["E3 — naive mirror", "echo (both error)", "0", "bus-off", "driven to ~240 TEC (error-passive)"]),
    row(["E5 — false-positive", "Parrot, no attacker", "—", "—", "no bus-off, max TEC = 0"]),
  ],
});

// ---- document --------------------------------------------------------------
const doc = new Document({
  creator: "Jose Rivera",
  title: "Parrot — CAN anti-spoofing defense (project report)",
  styles: {
    default: {
      document: { run: { font: FONT, size: 22 } },
      title: { run: { font: FONT, size: 40, bold: true, color: ACCENT }, paragraph: { spacing: { after: 80 } } },
      heading1: { run: { font: FONT, size: 28, bold: true, color: ACCENT }, paragraph: { spacing: { before: 260, after: 120 }, keepNext: true } },
      heading2: { run: { font: FONT, size: 23, bold: true, color: "2E2E2E" }, paragraph: { spacing: { before: 160, after: 80 }, keepNext: true } },
    },
  },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1080, bottom: 1080, left: 1260, right: 1260 } } },
    children: [
      new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun("Report — Cyber-Physical Systems and IoT Security")] }),
      p([{ text: "Reference paper: ", bold: true },
         { text: "Parrot, a software-only anti-spoofing defense system for the CAN bus.", italics: true }]),
      p([{ text: "Authors: ", bold: true }, "Jose Rivera and Daniel Carrasco Moreno."]),
      new Paragraph({
        spacing: { after: 160 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: ACCENT, space: 6 } },
        children: [new TextRun({ text: "Testbed: custom bit-level CAN simulator (Python). Code: https://github.com/sournachos/parrot", size: 18, color: "555555" })],
      }),

      h("Objectives", HeadingLevel.HEADING_1),
      p("We implement and evaluate Parrot, a software-only defense that ejects a spoofing node from the CAN bus by exploiting CAN's own error-handling, rather than by adding cryptography or changing the bus. Because the defense operates entirely in the data-link layer — arbitration, bit monitoring, the Transmit/Receive Error Counters (TEC/REC) and the bus-off state — which SocketCAN's vcan0 (used in the ICSim lab) does not model, we first built a bit-level CAN simulator to serve as the testbed. Our concrete objectives were:"),
      bullet("reproduce a CAN masquerade/spoofing attack on an unauthenticated bus;"),
      bullet("implement Parrot's detect-and-contend reaction on the legitimate owner of an ID;"),
      bullet("measure whether and how quickly it forces the attacker to bus-off, at what cost to the defender, and whether it produces false positives on legitimate traffic."),

      h("System Setup", HeadingLevel.HEADING_1),
      p([{ text: "Testbed (custom-made, described per the template's instructions). ", bold: true },
         "A discrete, bit-time model of one CAN segment at 500 kbit/s using standard 11-bit data frames, written from scratch in Python. We did not use vcan0 precisely because it forwards frames without modeling arbitration, error counters or bus-off, so Parrot's mechanism is invisible on it."]),
      p([{ text: "The simulator implements: ", bold: true },
         "the wired-AND bus (dominant 0 wins over recessive 1); full frame encoding (SOF, ID, RTR, control, data, CRC-15 with polynomial 0x4599, delimiters, ACK, EOF) with bit stuffing; bit-wise arbitration; bit monitoring (a transmitter that drives a recessive bit but reads back a dominant one loses arbitration inside the arbitration field, or detects a bit error after it); and the per-node error-counter state machine — TEC += 8 on a transmit error, TEC -= 1 on success, with thresholds TEC > 127 → error-passive and TEC > 255 → bus-off (ISO 11898-1)."]),
      p([{ text: "Nodes. ", bold: true },
         "EcuNode owns an ID and transmits a payload periodically; with the defense enabled it also runs Parrot. Attacker injects spoofed frames carrying a victim's ID at a fixed rate."]),
      p([{ text: "Parrot reaction. ", bold: true },
         "When a defended owner observes a frame carrying its own ID that it did not originate, it contends for that ID in the same bus slot with an all-dominant payload. Since it wins every data-bit contention, the spoofer reads back a dominant bit where it drove a recessive one, takes a bit error, and its TEC rises by 8 per contested injection."]),
      p([{ text: "Validation. ", bold: true },
         "A self-test asserts 13 properties of the layer before any result is trusted — including bus-off after exactly 32 transmit errors, error-passive at TEC = 128, bit stuffing never permitting six equal bits, lower ID winning arbitration, and an all-dominant owner beating a spoofer in the data field. All 13 pass. Well-known tools used: Python 3 and matplotlib (figures only)."]),

      h("Experiments", HeadingLevel.HEADING_1),
      p([{ text: "The code accompanies this report and is available at ", },
         { text: "https://github.com/sournachos/parrot", bold: true },
         { text: ": experiments.py runs every scenario and writes the CSV traces and summary.json; make_plots.py renders the figures; selftest.py validates the layer. The scenarios were:" }]),
      bullet([{ text: "E1 — Baseline, no defense. ", bold: true }, "Owner 0x120 transmits periodically; attacker spoofs 0x120."]),
      bullet([{ text: "E2 — Parrot ON. ", bold: true }, "Same configuration with the defense enabled."]),
      bullet([{ text: "E3 — Naive mirror. ", bold: true }, "The owner echoes, but both colliding transmitters take the error — to show why a naive echo is insufficient."]),
      bullet([{ text: "E4 — Injection-rate sweep. ", bold: true }, "Time-to-bus-off as the attacker's injection rate varies from 83 to 1250 frames/s."]),
      bullet([{ text: "E5 — False positives. ", bold: true }, "Three legitimate ECUs, Parrot active on each, no attacker."]),

      h("Results and Discussion", HeadingLevel.HEADING_1),
      resultsTable,
      new Paragraph({ spacing: { after: 60 } }),
      ...figure("fig1_attacker_tec.png", "Figure 1. Attacker TEC over time. With Parrot the counter rises 8 per contested injection and crosses the bus-off threshold after 32 injections (~94 ms); without a defense it stays healthy."),
      ...figure("fig2_defender_collateral.png", "Figure 2. Cost to the defender. Parrot keeps the owner near TEC = 0, whereas a naive mirror drags it to ~240 TEC (error-passive, near bus-off) as collateral damage."),
      ...figure("fig3_time_to_busoff.png", "Figure 3. Time-to-bus-off versus injection rate. It scales as ~32 / rate: a more aggressive attacker is ejected faster."),
      ...figure("fig4_spoof_success.png", "Figure 4. Spoofed frames delivered to the bus: 100 without a defense, 0 with Parrot.", 330, 257),

      h("Discussion", HeadingLevel.HEADING_2),
      p([{ text: "The attack works without the defense (E1). ", bold: true },
         "100 spoofed frames reach the bus and the attacker remains healthy — the direct consequence of CAN having no source authentication. This is the same missing property that makes the ICSim replay lab succeed."]),
      p([{ text: "Parrot ejects the attacker (E2, Figure 1). ", bold: true },
         "The attacker reaches bus-off after exactly 32 contested injections (~94 ms) and zero spoofed frames are delivered. The count matches first principles: ceil(256 / 8) = 32."]),
      p([{ text: "Parrot protects the defender; naive mirroring does not (Figure 2). ", bold: true },
         "Under Parrot the defender's TEC stays near zero because it wins each exchange (a successful transmit). A naive echo instead drives the defender to ~240 TEC — deep into error-passive — which is why the reaction must be asymmetric. This contrast is the core design insight."]),
      p([{ text: "Rate scaling (E4, Figure 3) and false positives (E5). ", bold: true },
         "Time-to-bus-off scales as ~32 / injection-rate, so the defense scales against its worst case, while injections-to-bus-off stays fixed at 32. With legitimate-only traffic and Parrot active on every ECU, no node ever reaches bus-off and all TECs remain 0 — Parrot reacts only to a foreign frame on an ID it owns."]),
      p([{ text: "Why spoofing/replay worked at all. ", bold: true },
         "No authentication, no source address, no freshness — the same properties raised in the ICSim lab's discussion questions. A MAC with a freshness value (AUTOSAR SecOC) would defeat the spoof outright but imposes per-frame computation and payload overhead; Parrot needs no bus changes, which is the point of it being 'software-only.'"]),

      h("Limitations and critical evaluation", HeadingLevel.HEADING_2),
      bullet("Error frames are represented by their counter effect, not emitted bit-for-bit. This is a standard testbed simplification and does not change the TEC/REC dynamics that cause bus-off."),
      bullet("The per-collision TEC bookkeeping is an explicit, tunable model (CollisionModel, with parrot and strict_both modes and adjustable deltas). The exact accounting must be reconciled with the reference paper's error-handling analysis; we expose it as a single parameter rather than hiding the assumption."),
      bullet("Timing is event-level (frame plus inter-frame space, in bit-times), not analog CAN-H/CAN-L waveforms — appropriate for counter and bus-off dynamics, not for physical-layer effects."),
      bullet("Per the course guidance, these are testbed-quality limitations rather than defects: the mechanism, the thresholds and the scaling all match first-principles CAN behaviour, and the self-test suite confirms the layer before any result is read."),
    ],
  }],
});

Packer.toBuffer(doc).then((b) => {
  fs.writeFileSync("/home/claude/parrot_sim/Parrot_Report.docx", b);
  console.log("wrote Parrot_Report.docx", b.length, "bytes");
});
