/* eslint-disable */
/**
 * Generated `api` utility.
 *
 * THIS CODE IS AUTOMATICALLY GENERATED.
 *
 * To regenerate, run `npx convex dev`.
 * @module
 */

import type * as audit from "../audit.js";
import type * as clerkWebhook from "../clerkWebhook.js";
import type * as cloudVersion from "../cloudVersion.js";
import type * as crons from "../crons.js";
import type * as dashboard from "../dashboard.js";
import type * as ecgData from "../ecgData.js";
import type * as fitness from "../fitness.js";
import type * as http from "../http.js";
import type * as lib_audit from "../lib/audit.js";
import type * as lib_auditIndex from "../lib/auditIndex.js";
import type * as lib_auth from "../lib/auth.js";
import type * as lib_clerkWebhook from "../lib/clerkWebhook.js";
import type * as lib_contract from "../lib/contract.js";
import type * as lib_crypto from "../lib/crypto.js";
import type * as lib_errors from "../lib/errors.js";
import type * as lib_feedback from "../lib/feedback.js";
import type * as lib_fitness from "../lib/fitness.js";
import type * as lib_machineAuth from "../lib/machineAuth.js";
import type * as lib_machineHealth from "../lib/machineHealth.js";
import type * as lib_machineHttpAuth from "../lib/machineHttpAuth.js";
import type * as lib_machineKeys from "../lib/machineKeys.js";
import type * as lib_machineRegistry from "../lib/machineRegistry.js";
import type * as lib_medicalParameters from "../lib/medicalParameters.js";
import type * as lib_notificationEmail from "../lib/notificationEmail.js";
import type * as lib_notificationVisibility from "../lib/notificationVisibility.js";
import type * as lib_notifications from "../lib/notifications.js";
import type * as lib_patientInvitation from "../lib/patientInvitation.js";
import type * as lib_rateLimits from "../lib/rateLimits.js";
import type * as lib_readableSessions from "../lib/readableSessions.js";
import type * as lib_recordStorage from "../lib/recordStorage.js";
import type * as lib_releaseValidation from "../lib/releaseValidation.js";
import type * as lib_sessionSummary from "../lib/sessionSummary.js";
import type * as lib_trainingPrivacy from "../lib/trainingPrivacy.js";
import type * as machineHealth from "../machineHealth.js";
import type * as machineKeys from "../machineKeys.js";
import type * as machineRegistry from "../machineRegistry.js";
import type * as machines from "../machines.js";
import type * as medicalParameters from "../medicalParameters.js";
import type * as medicalParametersStore from "../medicalParametersStore.js";
import type * as migrations_machineApiKeys from "../migrations/machineApiKeys.js";
import type * as migrations_multiOrganization from "../migrations/multiOrganization.js";
import type * as migrations_retireLegacyRecording from "../migrations/retireLegacyRecording.js";
import type * as notificationEmails from "../notificationEmails.js";
import type * as notifications from "../notifications.js";
import type * as organizations from "../organizations.js";
import type * as patientInvitations from "../patientInvitations.js";
import type * as recordHttp from "../recordHttp.js";
import type * as recordStorage from "../recordStorage.js";
import type * as recordVerification from "../recordVerification.js";
import type * as sessionFeedback from "../sessionFeedback.js";
import type * as sessionReports from "../sessionReports.js";
import type * as sessionReportsBackfill from "../sessionReportsBackfill.js";
import type * as sessionSummaries from "../sessionSummaries.js";
import type * as sessions from "../sessions.js";
import type * as softwareReleases from "../softwareReleases.js";
import type * as training from "../training.js";
import type * as users from "../users.js";

import type {
  ApiFromModules,
  FilterApi,
  FunctionReference,
} from "convex/server";

declare const fullApi: ApiFromModules<{
  audit: typeof audit;
  clerkWebhook: typeof clerkWebhook;
  cloudVersion: typeof cloudVersion;
  crons: typeof crons;
  dashboard: typeof dashboard;
  ecgData: typeof ecgData;
  fitness: typeof fitness;
  http: typeof http;
  "lib/audit": typeof lib_audit;
  "lib/auditIndex": typeof lib_auditIndex;
  "lib/auth": typeof lib_auth;
  "lib/clerkWebhook": typeof lib_clerkWebhook;
  "lib/contract": typeof lib_contract;
  "lib/crypto": typeof lib_crypto;
  "lib/errors": typeof lib_errors;
  "lib/feedback": typeof lib_feedback;
  "lib/fitness": typeof lib_fitness;
  "lib/machineAuth": typeof lib_machineAuth;
  "lib/machineHealth": typeof lib_machineHealth;
  "lib/machineHttpAuth": typeof lib_machineHttpAuth;
  "lib/machineKeys": typeof lib_machineKeys;
  "lib/machineRegistry": typeof lib_machineRegistry;
  "lib/medicalParameters": typeof lib_medicalParameters;
  "lib/notificationEmail": typeof lib_notificationEmail;
  "lib/notificationVisibility": typeof lib_notificationVisibility;
  "lib/notifications": typeof lib_notifications;
  "lib/patientInvitation": typeof lib_patientInvitation;
  "lib/rateLimits": typeof lib_rateLimits;
  "lib/readableSessions": typeof lib_readableSessions;
  "lib/recordStorage": typeof lib_recordStorage;
  "lib/releaseValidation": typeof lib_releaseValidation;
  "lib/sessionSummary": typeof lib_sessionSummary;
  "lib/trainingPrivacy": typeof lib_trainingPrivacy;
  machineHealth: typeof machineHealth;
  machineKeys: typeof machineKeys;
  machineRegistry: typeof machineRegistry;
  machines: typeof machines;
  medicalParameters: typeof medicalParameters;
  medicalParametersStore: typeof medicalParametersStore;
  "migrations/machineApiKeys": typeof migrations_machineApiKeys;
  "migrations/multiOrganization": typeof migrations_multiOrganization;
  "migrations/retireLegacyRecording": typeof migrations_retireLegacyRecording;
  notificationEmails: typeof notificationEmails;
  notifications: typeof notifications;
  organizations: typeof organizations;
  patientInvitations: typeof patientInvitations;
  recordHttp: typeof recordHttp;
  recordStorage: typeof recordStorage;
  recordVerification: typeof recordVerification;
  sessionFeedback: typeof sessionFeedback;
  sessionReports: typeof sessionReports;
  sessionReportsBackfill: typeof sessionReportsBackfill;
  sessionSummaries: typeof sessionSummaries;
  sessions: typeof sessions;
  softwareReleases: typeof softwareReleases;
  training: typeof training;
  users: typeof users;
}>;

/**
 * A utility for referencing Convex functions in your app's public API.
 *
 * Usage:
 * ```js
 * const myFunctionReference = api.myModule.myFunction;
 * ```
 */
export declare const api: FilterApi<
  typeof fullApi,
  FunctionReference<any, "public">
>;

/**
 * A utility for referencing Convex functions in your app's internal API.
 *
 * Usage:
 * ```js
 * const myFunctionReference = internal.myModule.myFunction;
 * ```
 */
export declare const internal: FilterApi<
  typeof fullApi,
  FunctionReference<any, "internal">
>;

export declare const components: {
  rateLimiter: import("@convex-dev/rate-limiter/_generated/component.js").ComponentApi<"rateLimiter">;
};
