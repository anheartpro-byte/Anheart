/* eslint-disable */
/**
 * Generated `api` utility.
 *
 * THIS CODE IS AUTOMATICALLY GENERATED.
 *
 * To regenerate, run `npx convex dev`.
 * @module
 */

import type * as clerkWebhook from "../clerkWebhook.js";
import type * as cloudVersion from "../cloudVersion.js";
import type * as crons from "../crons.js";
import type * as dashboard from "../dashboard.js";
import type * as ecgData from "../ecgData.js";
import type * as http from "../http.js";
import type * as lib_auth from "../lib/auth.js";
import type * as lib_clerkWebhook from "../lib/clerkWebhook.js";
import type * as lib_contract from "../lib/contract.js";
import type * as lib_crypto from "../lib/crypto.js";
import type * as lib_machineAuth from "../lib/machineAuth.js";
import type * as lib_machineHealth from "../lib/machineHealth.js";
import type * as lib_machineHttpAuth from "../lib/machineHttpAuth.js";
import type * as lib_patientInvitation from "../lib/patientInvitation.js";
import type * as lib_rateLimits from "../lib/rateLimits.js";
import type * as lib_recordStorage from "../lib/recordStorage.js";
import type * as lib_releaseValidation from "../lib/releaseValidation.js";
import type * as lib_trainingPrivacy from "../lib/trainingPrivacy.js";
import type * as machineHealth from "../machineHealth.js";
import type * as machines from "../machines.js";
import type * as migrations_multiOrganization from "../migrations/multiOrganization.js";
import type * as migrations_retireLegacyRecording from "../migrations/retireLegacyRecording.js";
import type * as organizations from "../organizations.js";
import type * as patientInvitations from "../patientInvitations.js";
import type * as recordHttp from "../recordHttp.js";
import type * as recordStorage from "../recordStorage.js";
import type * as recordVerification from "../recordVerification.js";
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
  clerkWebhook: typeof clerkWebhook;
  cloudVersion: typeof cloudVersion;
  crons: typeof crons;
  dashboard: typeof dashboard;
  ecgData: typeof ecgData;
  http: typeof http;
  "lib/auth": typeof lib_auth;
  "lib/clerkWebhook": typeof lib_clerkWebhook;
  "lib/contract": typeof lib_contract;
  "lib/crypto": typeof lib_crypto;
  "lib/machineAuth": typeof lib_machineAuth;
  "lib/machineHealth": typeof lib_machineHealth;
  "lib/machineHttpAuth": typeof lib_machineHttpAuth;
  "lib/patientInvitation": typeof lib_patientInvitation;
  "lib/rateLimits": typeof lib_rateLimits;
  "lib/recordStorage": typeof lib_recordStorage;
  "lib/releaseValidation": typeof lib_releaseValidation;
  "lib/trainingPrivacy": typeof lib_trainingPrivacy;
  machineHealth: typeof machineHealth;
  machines: typeof machines;
  "migrations/multiOrganization": typeof migrations_multiOrganization;
  "migrations/retireLegacyRecording": typeof migrations_retireLegacyRecording;
  organizations: typeof organizations;
  patientInvitations: typeof patientInvitations;
  recordHttp: typeof recordHttp;
  recordStorage: typeof recordStorage;
  recordVerification: typeof recordVerification;
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
