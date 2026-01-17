# Task 5.1: PDF Report Generation

## Objective

Generate PDF reports for completed ECG sessions containing patient info, session metrics, and ECG overview chart.

## Dependencies

- Phase 3 (Session Summaries) completed

---

## Acceptance Criteria

### Report Content

- [ ] Patient information (name, email)
- [ ] Session information (date, duration, machine)
- [ ] Heart rate metrics (avg, min, max, HRV)
- [ ] ECG overview chart (downsampled)
- [ ] Technician notes (if any)
- [ ] Generated timestamp

### Report Action

- [ ] `generateReport` internalAction created
- [ ] Generates PDF buffer
- [ ] Uploads to Convex storage
- [ ] Links storage ID to session summary
- [ ] Handles errors gracefully

### Report Query

- [ ] `getReportUrl` query returns signed URL
- [ ] URL valid for limited time
- [ ] Respects access control

---

## Implementation

```bash
npm install @react-pdf/renderer
```

```typescript
// convex/reports.ts
"use node";

import { internalAction, internalMutation, query } from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";
import { getCurrentUserOrThrow, canAccessMachine } from "./lib/auth";

// Note: PDF generation runs in Node.js runtime
import {
  Document,
  Page,
  Text,
  View,
  StyleSheet,
  renderToBuffer,
} from "@react-pdf/renderer";
import React from "react";

// PDF Styles
const styles = StyleSheet.create({
  page: {
    padding: 40,
    fontFamily: "Helvetica",
  },
  header: {
    marginBottom: 20,
    borderBottom: "1 solid #ccc",
    paddingBottom: 10,
  },
  title: {
    fontSize: 24,
    fontWeight: "bold",
    marginBottom: 5,
  },
  subtitle: {
    fontSize: 12,
    color: "#666",
  },
  section: {
    marginBottom: 20,
  },
  sectionTitle: {
    fontSize: 14,
    fontWeight: "bold",
    marginBottom: 10,
    backgroundColor: "#f0f0f0",
    padding: 5,
  },
  row: {
    flexDirection: "row",
    marginBottom: 5,
  },
  label: {
    width: 150,
    fontSize: 10,
    color: "#666",
  },
  value: {
    flex: 1,
    fontSize: 10,
  },
  metricsGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
  },
  metricBox: {
    width: "25%",
    padding: 10,
    alignItems: "center",
  },
  metricValue: {
    fontSize: 24,
    fontWeight: "bold",
    color: "#2563eb",
  },
  metricLabel: {
    fontSize: 8,
    color: "#666",
    marginTop: 2,
  },
  footer: {
    position: "absolute",
    bottom: 30,
    left: 40,
    right: 40,
    fontSize: 8,
    color: "#999",
    textAlign: "center",
  },
  notes: {
    fontSize: 10,
    backgroundColor: "#f9f9f9",
    padding: 10,
    marginTop: 5,
  },
});

// PDF Document Component
function SessionReportPDF({
  patient,
  session,
  summary,
  machine,
}: {
  patient: { firstName: string; lastName: string; email: string };
  session: {
    startedAt: number;
    endedAt?: number;
    channels: string[];
    notes?: string;
  };
  summary: {
    duration: number;
    metrics: {
      avgHeartRate: number;
      minHeartRate: number;
      maxHeartRate: number;
      hrv?: number;
    };
  };
  machine: { name: string };
}) {
  const formatDate = (ts: number) => new Date(ts).toLocaleString();
  const formatDuration = (secs: number) => {
    const mins = Math.floor(secs / 60);
    const hrs = Math.floor(mins / 60);
    if (hrs > 0) return `${hrs}h ${mins % 60}m`;
    return `${mins}m ${secs % 60}s`;
  };

  return React.createElement(
    Document,
    {},
    React.createElement(
      Page,
      { size: "A4", style: styles.page },
      // Header
      React.createElement(
        View,
        { style: styles.header },
        React.createElement(
          Text,
          { style: styles.title },
          "ECG Session Report",
        ),
        React.createElement(
          Text,
          { style: styles.subtitle },
          `Generated: ${formatDate(Date.now())}`,
        ),
      ),
      // Patient Info
      React.createElement(
        View,
        { style: styles.section },
        React.createElement(
          Text,
          { style: styles.sectionTitle },
          "Patient Information",
        ),
        React.createElement(
          View,
          { style: styles.row },
          React.createElement(Text, { style: styles.label }, "Name:"),
          React.createElement(
            Text,
            { style: styles.value },
            `${patient.firstName} ${patient.lastName}`,
          ),
        ),
        React.createElement(
          View,
          { style: styles.row },
          React.createElement(Text, { style: styles.label }, "Email:"),
          React.createElement(Text, { style: styles.value }, patient.email),
        ),
      ),
      // Session Info
      React.createElement(
        View,
        { style: styles.section },
        React.createElement(
          Text,
          { style: styles.sectionTitle },
          "Session Information",
        ),
        React.createElement(
          View,
          { style: styles.row },
          React.createElement(Text, { style: styles.label }, "Machine:"),
          React.createElement(Text, { style: styles.value }, machine.name),
        ),
        React.createElement(
          View,
          { style: styles.row },
          React.createElement(Text, { style: styles.label }, "Started:"),
          React.createElement(
            Text,
            { style: styles.value },
            formatDate(session.startedAt),
          ),
        ),
        React.createElement(
          View,
          { style: styles.row },
          React.createElement(Text, { style: styles.label }, "Duration:"),
          React.createElement(
            Text,
            { style: styles.value },
            formatDuration(summary.duration),
          ),
        ),
        React.createElement(
          View,
          { style: styles.row },
          React.createElement(Text, { style: styles.label }, "Channels:"),
          React.createElement(
            Text,
            { style: styles.value },
            session.channels.join(", "),
          ),
        ),
      ),
      // Metrics
      React.createElement(
        View,
        { style: styles.section },
        React.createElement(
          Text,
          { style: styles.sectionTitle },
          "Heart Rate Metrics",
        ),
        React.createElement(
          View,
          { style: styles.metricsGrid },
          React.createElement(
            View,
            { style: styles.metricBox },
            React.createElement(
              Text,
              { style: styles.metricValue },
              String(summary.metrics.avgHeartRate),
            ),
            React.createElement(
              Text,
              { style: styles.metricLabel },
              "Average BPM",
            ),
          ),
          React.createElement(
            View,
            { style: styles.metricBox },
            React.createElement(
              Text,
              { style: styles.metricValue },
              String(summary.metrics.minHeartRate),
            ),
            React.createElement(Text, { style: styles.metricLabel }, "Min BPM"),
          ),
          React.createElement(
            View,
            { style: styles.metricBox },
            React.createElement(
              Text,
              { style: styles.metricValue },
              String(summary.metrics.maxHeartRate),
            ),
            React.createElement(Text, { style: styles.metricLabel }, "Max BPM"),
          ),
          React.createElement(
            View,
            { style: styles.metricBox },
            React.createElement(
              Text,
              { style: styles.metricValue },
              String(summary.metrics.hrv ?? "-"),
            ),
            React.createElement(
              Text,
              { style: styles.metricLabel },
              "HRV (ms)",
            ),
          ),
        ),
      ),
      // Notes
      session.notes &&
        React.createElement(
          View,
          { style: styles.section },
          React.createElement(Text, { style: styles.sectionTitle }, "Notes"),
          React.createElement(Text, { style: styles.notes }, session.notes),
        ),
      // Footer
      React.createElement(
        Text,
        { style: styles.footer },
        "AnHeart ECG Monitoring System - This report is for medical reference only",
      ),
    ),
  );
}

/**
 * Generate PDF report for a session
 */
export const generateReport = internalAction({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.union(v.id("_storage"), v.null()),
  handler: async (ctx, args) => {
    console.log(`Generating PDF report for session ${args.sessionId}`);

    // Get session data
    const session: any = await ctx.runQuery(
      internal.reports.getSessionForReport,
      {
        sessionId: args.sessionId,
      },
    );

    if (!session) {
      console.error("Session not found");
      return null;
    }

    // Get summary
    const summary: any = await ctx.runQuery(
      internal.reports.getSummaryForReport,
      {
        sessionId: args.sessionId,
      },
    );

    if (!summary) {
      console.error("Summary not found");
      return null;
    }

    // Generate PDF
    const pdfBuffer = await renderToBuffer(
      React.createElement(SessionReportPDF, {
        patient: session.patient,
        session: {
          startedAt: session.startedAt,
          endedAt: session.endedAt,
          channels: session.channels,
          notes: session.notes,
        },
        summary: {
          duration: summary.duration,
          metrics: summary.metrics,
        },
        machine: session.machine,
      }),
    );

    // Upload to storage
    const storageId = await ctx.storage.store(
      new Blob([pdfBuffer], { type: "application/pdf" }),
    );

    // Link to summary
    await ctx.runMutation(internal.reports.linkReportToSummary, {
      sessionId: args.sessionId,
      storageId,
    });

    console.log(`Report generated: ${storageId}`);
    return storageId;
  },
});

// Internal queries for report generation
export const getSessionForReport = internalQuery({
  args: { sessionId: v.id("sessions") },
  returns: v.any(),
  handler: async (ctx, args) => {
    const session = await ctx.db.get(args.sessionId);
    if (!session) return null;

    const patient = await ctx.db.get(session.userId);
    const machine = await ctx.db.get(session.machineId);

    return {
      ...session,
      patient: patient
        ? {
            firstName: patient.firstName,
            lastName: patient.lastName,
            email: patient.email,
          }
        : null,
      machine: machine ? { name: machine.name } : null,
    };
  },
});

export const getSummaryForReport = internalQuery({
  args: { sessionId: v.id("sessions") },
  returns: v.any(),
  handler: async (ctx, args) => {
    return await ctx.db
      .query("session_summaries")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .unique();
  },
});

export const linkReportToSummary = internalMutation({
  args: {
    sessionId: v.id("sessions"),
    storageId: v.id("_storage"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const summary = await ctx.db
      .query("session_summaries")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .unique();

    if (summary) {
      await ctx.db.patch(summary._id, { reportFileId: args.storageId });
    }
    return null;
  },
});

/**
 * Get report download URL
 */
export const getReportUrl = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(v.string(), v.null()),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Check access
    const session = await ctx.db.get(args.sessionId);
    if (!session) return null;

    const isPatient = currentUser._id === session.userId;
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !canAccessMach && currentUser.role !== "admin") {
      return null;
    }

    // Get summary with report
    const summary = await ctx.db
      .query("session_summaries")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .unique();

    if (!summary?.reportFileId) {
      return null;
    }

    return await ctx.storage.getUrl(summary.reportFileId);
  },
});
```

---

## Testing Steps

1. Complete a session with data
2. Verify summary generated
3. Trigger report generation (manual or automatic)
4. Query report URL
5. Download and open PDF
6. Verify all content correct
7. Test access control - non-authorized user gets null
