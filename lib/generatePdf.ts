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
  };
  ecgSamples?: Record<string, number[]>;
}

export function generateSessionPdf(data: SessionReportData): void {
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
  doc.text("ECG Session Report", 14, 22);

  doc.setFontSize(10);
  doc.setFont("helvetica", "normal");
  doc.text(`Report ID: ${data.sessionId.slice(-8).toUpperCase()}`, 14, 30);
  doc.text(`Generated: ${new Date().toLocaleString()}`, pageWidth - 14, 30, {
    align: "right",
  });

  // Reset text color
  doc.setTextColor(...textColor);

  let yPos = 50;

  // Patient Information Section
  doc.setFontSize(14);
  doc.setFont("helvetica", "bold");
  doc.text("Patient Information", 14, yPos);
  yPos += 8;

  doc.setFontSize(10);
  doc.setFont("helvetica", "normal");

  autoTable(doc, {
    startY: yPos,
    head: [],
    body: [
      ["Name", data.patientName],
      ["Email", data.patientEmail],
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
  doc.text("Session Details", 14, yPos);
  yPos += 8;

  const startDate = new Date(data.startedAt);
  const endDate = data.endedAt ? new Date(data.endedAt) : null;
  const duration = data.endedAt
    ? Math.round((data.endedAt - data.startedAt) / 1000)
    : 0;
  const durationStr =
    duration > 0 ? `${Math.floor(duration / 60)}m ${duration % 60}s` : "N/A";

  autoTable(doc, {
    startY: yPos,
    head: [],
    body: [
      ["Machine", data.machineName],
      ["Started", startDate.toLocaleString()],
      ["Ended", endDate ? endDate.toLocaleString() : "N/A"],
      ["Duration", durationStr],
      ["Channels", data.channels.join(", ")],
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
    doc.text("Recording Statistics", 14, yPos);
    yPos += 8;

    autoTable(doc, {
      startY: yPos,
      head: [],
      body: [
        ["Total Data Batches", data.ecgStats.totalBatches.toString()],
        ["Recording Duration", `${data.ecgStats.durationSeconds} seconds`],
        ["Sample Rate", "100 Hz"],
        ["Total Samples", `~${data.ecgStats.totalBatches * 100}`],
      ],
      theme: "plain",
      styles: { fontSize: 10, cellPadding: 3 },
      columnStyles: {
        0: { fontStyle: "bold", cellWidth: 50 },
        1: { cellWidth: 80 },
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
    doc.text("ECG Waveform Preview", 14, yPos);
    yPos += 10;

    for (const [channel, samples] of Object.entries(data.ecgSamples)) {
      if (samples.length === 0) continue;

      doc.setFontSize(10);
      doc.setFont("helvetica", "normal");
      doc.text(`${channel} Channel (first 500 samples)`, 14, yPos);
      yPos += 5;

      // Draw a simple waveform
      const graphWidth = pageWidth - 28;
      const graphHeight = 30;
      const displaySamples = samples.slice(0, 500);

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
    doc.text("Notes", 14, yPos);
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
      `AnHeart ECG Monitoring System - Page ${i} of ${pageCount}`,
      pageWidth / 2,
      doc.internal.pageSize.getHeight() - 10,
      { align: "center" },
    );
  }

  // Save the PDF
  const filename = `ECG_Report_${data.sessionId.slice(-8).toUpperCase()}_${new Date().toISOString().split("T")[0]}.pdf`;
  doc.save(filename);
}
