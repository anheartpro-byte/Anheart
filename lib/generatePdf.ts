import { jsPDF } from "jspdf";
import autoTable from "jspdf-autotable";

interface SessionReportData {
  sessionId: string;
  patientName: string;
  patientEmail: string;
  machineName: string;
  startedAt: number;
  endedAt?: number;
  channels: string[];
  notes?: string;
  ecgStats?: {
    totalBatches: number;
    durationSeconds: number;
    channels: string[];
    /** Rates (Hz) read from the loaded batches; empty when they carry none. */
    sampleRates: number[];
    /** Samples counted on the loaded batches, and how many batches that was. */
    countedSamples: number;
    countedBatches: number;
  };
  ecgSamples?: Record<string, number[]>;
}

interface SessionReportOptions {
  /** Translator bound to the `reports.pdf` messages. */
  t: (key: string, values?: Record<string, string | number>) => string;
  /** UI locale, used for dates and numbers. */
  locale: string;
}

/**
 * jsPDF's built-in fonts only cover Latin-1, and one character outside it
 * garbles the whole string. Locale formatting inserts narrow no-break spaces
 * (French thousands separator), so they are replaced by plain spaces.
 */
function pdfText(text: string): string {
  return text.replace(/[  ]/g, " ").replace(/’/g, "'");
}

export function buildSessionPdf(
  data: SessionReportData,
  { t, locale }: SessionReportOptions,
): jsPDF {
  const label = (key: string, values?: Record<string, string | number>) =>
    pdfText(t(key, values));
  const formatNumber = (value: number) =>
    pdfText(new Intl.NumberFormat(locale).format(value));
  const formatDateTime = (value: number | Date) =>
    pdfText(new Date(value).toLocaleString(locale));
  const reportId = data.sessionId.slice(-8).toUpperCase();

  const doc = new jsPDF();
  const pageWidth = doc.internal.pageSize.getWidth();

  // Colors
  const primaryColor: [number, number, number] = [34, 197, 94]; // Green
  const textColor: [number, number, number] = [31, 41, 55]; // Dark gray

  // Header
  doc.setFillColor(...primaryColor);
  doc.rect(0, 0, pageWidth, 35, "F");

  doc.setTextColor(255, 255, 255);
  doc.setFontSize(24);
  doc.setFont("helvetica", "bold");
  doc.text(label("title"), 14, 22);

  doc.setFontSize(10);
  doc.setFont("helvetica", "normal");
  doc.text(label("reportId", { id: reportId }), 14, 30);
  doc.text(
    label("generatedAt", { date: formatDateTime(new Date()) }),
    pageWidth - 14,
    30,
    {
      align: "right",
    },
  );

  // Reset text color
  doc.setTextColor(...textColor);

  let yPos = 50;

  // Patient Information Section
  doc.setFontSize(14);
  doc.setFont("helvetica", "bold");
  doc.text(label("patientInfo"), 14, yPos);
  yPos += 8;

  doc.setFontSize(10);
  doc.setFont("helvetica", "normal");

  autoTable(doc, {
    startY: yPos,
    head: [],
    body: [
      [label("name"), data.patientName],
      [label("email"), data.patientEmail],
    ],
    theme: "plain",
    styles: { fontSize: 10, cellPadding: 3 },
    columnStyles: {
      0: { fontStyle: "bold", cellWidth: 40 },
      1: { cellWidth: 100 },
    },
    margin: { left: 14 },
  });

  yPos =
    (doc as jsPDF & { lastAutoTable: { finalY: number } }).lastAutoTable
      .finalY + 15;

  // Session Information Section
  doc.setFontSize(14);
  doc.setFont("helvetica", "bold");
  doc.text(label("sessionDetails"), 14, yPos);
  yPos += 8;

  const duration = data.endedAt
    ? Math.round((data.endedAt - data.startedAt) / 1000)
    : 0;
  const durationStr =
    duration > 0
      ? label("durationValue", {
          minutes: Math.floor(duration / 60),
          seconds: duration % 60,
        })
      : label("notAvailable");

  autoTable(doc, {
    startY: yPos,
    head: [],
    body: [
      [label("machine"), data.machineName],
      [label("started"), formatDateTime(data.startedAt)],
      [
        label("ended"),
        data.endedAt ? formatDateTime(data.endedAt) : label("notAvailable"),
      ],
      [label("duration"), durationStr],
      [label("channels"), data.channels.join(", ")],
    ],
    theme: "plain",
    styles: { fontSize: 10, cellPadding: 3 },
    columnStyles: {
      0: { fontStyle: "bold", cellWidth: 40 },
      1: { cellWidth: 100 },
    },
    margin: { left: 14 },
  });

  yPos =
    (doc as jsPDF & { lastAutoTable: { finalY: number } }).lastAutoTable
      .finalY + 15;

  // ECG Data Statistics
  if (data.ecgStats) {
    doc.setFontSize(14);
    doc.setFont("helvetica", "bold");
    doc.text(label("statistics"), 14, yPos);
    yPos += 8;

    // Rate and sample count come from the recorded batches. Only part of a
    // long recording is loaded for the report, so a partial count says so.
    const { sampleRates, countedSamples, countedBatches, totalBatches } =
      data.ecgStats;
    const sampleRate =
      sampleRates.length > 0
        ? label("sampleRateValue", { rate: sampleRates.join(" / ") })
        : label("sampleRateUnknown");
    const samples =
      totalBatches > countedBatches
        ? label("samplesPartial", {
            count: formatNumber(countedSamples),
            loaded: formatNumber(countedBatches),
            total: formatNumber(totalBatches),
          })
        : formatNumber(countedSamples);

    autoTable(doc, {
      startY: yPos,
      head: [],
      body: [
        [label("totalBatches"), formatNumber(totalBatches)],
        [
          label("recordingDuration"),
          label("recordingDurationValue", {
            seconds: formatNumber(data.ecgStats.durationSeconds),
          }),
        ],
        [label("sampleRate"), sampleRate],
        [label("samples"), samples],
      ],
      theme: "plain",
      styles: { fontSize: 10, cellPadding: 3 },
      columnStyles: {
        0: { fontStyle: "bold", cellWidth: 60 },
        1: { cellWidth: 110 },
      },
      margin: { left: 14 },
    });

    yPos =
      (doc as jsPDF & { lastAutoTable: { finalY: number } }).lastAutoTable
        .finalY + 15;
  }

  // ECG Waveform Preview (simple representation)
  if (data.ecgSamples && Object.keys(data.ecgSamples).length > 0) {
    // Check if we need a new page
    if (yPos > 200) {
      doc.addPage();
      yPos = 20;
    }

    doc.setFontSize(14);
    doc.setFont("helvetica", "bold");
    doc.text(label("waveformPreview"), 14, yPos);
    yPos += 10;

    for (const [channel, samples] of Object.entries(data.ecgSamples)) {
      if (samples.length === 0) continue;

      const displaySamples = samples.slice(0, 500);

      doc.setFontSize(10);
      doc.setFont("helvetica", "normal");
      doc.text(
        label("channelPreview", {
          channel,
          count: formatNumber(displaySamples.length),
        }),
        14,
        yPos,
      );
      yPos += 5;

      // Draw a simple waveform
      const graphWidth = pageWidth - 28;
      const graphHeight = 30;

      // Draw border
      doc.setDrawColor(200, 200, 200);
      doc.rect(14, yPos, graphWidth, graphHeight);

      // Draw grid
      doc.setDrawColor(230, 230, 230);
      for (let i = 1; i < 5; i++) {
        doc.line(
          14,
          yPos + (graphHeight / 5) * i,
          14 + graphWidth,
          yPos + (graphHeight / 5) * i,
        );
      }
      for (let i = 1; i < 10; i++) {
        doc.line(
          14 + (graphWidth / 10) * i,
          yPos,
          14 + (graphWidth / 10) * i,
          yPos + graphHeight,
        );
      }

      // Normalize and draw waveform
      const minVal = Math.min(...displaySamples);
      const maxVal = Math.max(...displaySamples);
      const range = maxVal - minVal || 1;

      doc.setDrawColor(...primaryColor);
      doc.setLineWidth(0.3);

      const step = graphWidth / displaySamples.length;
      let prevX = 14;
      let prevY =
        yPos +
        graphHeight -
        ((displaySamples[0] - minVal) / range) * graphHeight;

      for (let i = 1; i < displaySamples.length; i++) {
        const x = 14 + i * step;
        const y =
          yPos +
          graphHeight -
          ((displaySamples[i] - minVal) / range) * graphHeight;
        doc.line(prevX, prevY, x, y);
        prevX = x;
        prevY = y;
      }

      yPos += graphHeight + 15;

      // Check for page break
      if (yPos > 260) {
        doc.addPage();
        yPos = 20;
      }
    }
  }

  // Notes Section
  if (data.notes) {
    if (yPos > 240) {
      doc.addPage();
      yPos = 20;
    }

    doc.setFontSize(14);
    doc.setFont("helvetica", "bold");
    doc.text(label("notes"), 14, yPos);
    yPos += 8;

    doc.setFontSize(10);
    doc.setFont("helvetica", "normal");

    // Word wrap the notes
    const splitNotes = doc.splitTextToSize(data.notes, pageWidth - 28);
    doc.text(splitNotes, 14, yPos);
  }

  // Footer on each page
  const pageCount = doc.getNumberOfPages();
  for (let i = 1; i <= pageCount; i++) {
    doc.setPage(i);
    doc.setFontSize(8);
    doc.setTextColor(150, 150, 150);
    doc.text(
      label("footer", { page: i, total: pageCount }),
      pageWidth / 2,
      doc.internal.pageSize.getHeight() - 10,
      { align: "center" },
    );
  }

  return doc;
}

export function generateSessionPdf(
  data: SessionReportData,
  options: SessionReportOptions,
): void {
  const doc = buildSessionPdf(data, options);

  // Save the PDF
  const filename = options.t("fileName", {
    id: data.sessionId.slice(-8).toUpperCase(),
    date: new Date().toISOString().split("T")[0],
  });
  doc.save(filename);
}
