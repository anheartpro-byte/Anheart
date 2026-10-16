/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ts from "typescript";
import http from "./http";
import { api, internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import {
  AUDIT_ACTIONS,
  SENSITIVE_MUTATIONS,
  type AuditAction,
} from "./lib/audit";
import { CURRENT_QUESTIONNAIRE } from "../lib/fitnessQuestionnaire";
import { CONSENT_TEXT_VERSION } from "../lib/consentText";
import { registryRelease, registryUpload } from "./machineRegistry.fixtures";
import { mockClerkInvitations } from "./patientInvitation.fixtures";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import {
  addSession,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  ORG_A_CLERK_ORG,
  ORG_B_CLERK_ORG,
  registerShippedMedicalParameters,
  backendOf,
  seedLaunchWorld,
  seedMachineWorld,
  type World,
} from "./test.setup";

const rows = (w: Pick<World, "t">) =>
  w.t.run((ctx) => ctx.db.query("audit_log").collect());

async function unchanged(w: Pick<World, "t">, call: () => Promise<unknown>) {
  const before = await rows(w);
  await call();
  expect(await rows(w)).toEqual(before);
}

type MachineWorld = Awaited<ReturnType<typeof seedMachineWorld>>;
function machinePost(w: MachineWorld, path: string, body: unknown) {
  return w.t.fetch(`/api/machine/training/${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${w.machineKey}`,
      "Content-Type": "application/json",
      [CONTRACT_HEADER]: CONTRACT_VERSION,
    },
    body: JSON.stringify(body),
  });
}

function expectEntry(
  actual: Doc<"audit_log"> | undefined,
  expected: Pick<
    Doc<"audit_log">,
    "action" | "source" | "actorRole" | "targetType" | "targetId"
  > &
    Pick<Partial<Doc<"audit_log">>, "actorId" | "organizationId" | "details">,
) {
  const { actorId, organizationId, ...required } = expected;
  expect(actual).toMatchObject(required);
  expect(actual?.actorId).toBe(actorId);
  expect(actual?.organizationId).toBe(organizationId);
  expect(actual?.requestId.length).toBeGreaterThan(0);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  configureAnheartOrganization(null);
});

type Expected = Parameters<typeof expectEntry>[1];
function site(
  w: World,
  action: AuditAction,
  targetId: string,
  targetType: Expected["targetType"],
  actor: "admin" | "orgAdmin" | "manager" | "patient" = "admin",
  organizationId: Id<"organizations"> = w.orgA,
  details?: Expected["details"],
): Expected {
  const roles = {
    admin: "admin",
    orgAdmin: "org_admin",
    manager: "gestionnaire",
    patient: "user",
  } as const;
  return {
    action,
    targetId,
    targetType,
    actorId: w[actor],
    actorRole: roles[actor],
    source: "site",
    organizationId,
    ...(details === undefined ? {} : { details }),
  };
}
function system(
  action: AuditAction,
  targetId: string,
  targetType: Expected["targetType"],
  source: "operator" | "cron" | "webhook",
  organizationId?: Id<"organizations">,
  details?: Expected["details"],
): Expected {
  return {
    action,
    targetId,
    targetType,
    source,
    organizationId,
    actorRole: "system",
    ...(details === undefined ? {} : { details }),
  };
}
async function observe(w: Pick<World, "t">, call: () => Promise<Expected[]>) {
  const before = new Set((await rows(w)).map((row) => row._id));
  const expected = await call();
  const written = (await rows(w)).filter((row) => !before.has(row._id));
  expect(written).toHaveLength(expected.length);
  for (const entry of expected) {
    expectEntry(
      written.find(
        (row) =>
          row.action === entry.action &&
          row.targetId === entry.targetId &&
          row.organizationId === entry.organizationId,
      ),
      entry,
    );
  }
  return written;
}
async function key(w: World) {
  return await as(w.t, "admin").mutation(api.machineKeys.createMachineKey, {
    machineId: w.machine,
  });
}
async function benchReview(w: World) {
  await registryRelease(w);
  await as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
    machineId: w.machine,
    level: "bench",
    softwareVersion: "pi-0.1.0",
    medicalParametersVersion: null,
    reportStorageId: await registryUpload(w),
  });
}

function membershipEvent(role: "admin" | "gestionnaire" | "user" = "user") {
  return {
    kind: "membership" as const,
    deleted: false,
    organization: {
      id: ORG_A_CLERK_ORG,
      name: "Synthetic centre",
      slug: "centre-a",
      createdAt: NOW,
      updatedAt: NOW,
    },
    occurredAt: NOW + 1,
    updatedAt: NOW + 1,
    membershipId: "synthetic-c1-membership",
    userId: "synthetic-c1-account",
    firstName: "Synthetic",
    lastName: "Member",
    email: "synthetic-c1@example.invalid",
    role,
  };
}

async function acceptInvitation(w: World) {
  mockClerkInvitations();
  const sent = await as(w.t, "manager").action(api.users.createPatient, {
    firstName: "Synthetic",
    lastName: "Invitee",
    email: "synthetic-c1@example.invalid",
  });
  const invitation = await w.t.run((ctx) => ctx.db.get(sent.invitationId));
  const userId = invitation?.userId;
  if (!userId) throw new Error("Missing synthetic invited profile");
  const event = {
    ...membershipEvent(),
    verifiedEmails: ["synthetic-c1@example.invalid"],
  };
  const written = await observe(w, async () => {
    await w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId: "c1-invitation",
      event,
    });
    return [
      system("membership.created", userId, "user", "webhook", w.orgA, {
        role: "user",
      }),
      system("user.linked_to_account", userId, "user", "webhook", w.orgA),
      system(
        "patient_gestionnaire.assigned",
        userId,
        "user",
        "webhook",
        w.orgA,
        { gestionnaireId: w.manager },
      ),
    ];
  });
  expect(await w.t.run((ctx) => ctx.db.get(sent.invitationId))).toMatchObject({
    status: "accepted",
  });
  expect(await w.t.run((ctx) => ctx.db.get(userId))).toMatchObject({
    clerkId: event.userId,
  });
  expect(
    await w.t.run((ctx) =>
      ctx.db
        .query("user_gestionnaires")
        .withIndex("by_user", (q) => q.eq("userId", userId))
        .collect(),
    ),
  ).toMatchObject([{ gestionnaireId: w.manager, organizationId: w.orgA }]);
  await unchanged(w, () =>
    w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId: "c1-invitation",
      event,
    }),
  );
  return written;
}

const SCENARIOS: Record<string, (w: World) => Promise<Doc<"audit_log">[]>> = {
  "clerkWebhook.applyEvent": async (w) => {
    const written: Doc<"audit_log">[] = await acceptInvitation(w);
    const organization = {
      id: ORG_A_CLERK_ORG,
      name: "Synthetic centre",
      slug: "centre-a",
      createdAt: NOW,
      updatedAt: NOW,
    };
    const member = {
      kind: "membership",
      deleted: false,
      organization,
      occurredAt: NOW + 1,
      updatedAt: NOW + 1,
      membershipId: "synthetic-audit-member",
      userId: "synthetic-audit-account",
      firstName: "Synthetic",
      lastName: "Member",
      email: "audit-member@example.invalid",
      role: "user",
    } as const;
    let memberId: Id<"users">;
    written.push(
      ...(await observe(w, async () => {
        await w.t.mutation(internal.clerkWebhook.applyEvent, {
          eventId: "audit-member-create",
          event: member,
        });
        const user = await w.t.run((ctx) =>
          ctx.db
            .query("users")
            .withIndex("by_clerk_id", (q) => q.eq("clerkId", member.userId))
            .unique(),
        );
        if (!user) throw new Error("Missing synthetic member");
        memberId = user._id;
        return [
          system("membership.created", memberId, "user", "webhook", w.orgA, {
            role: "user",
          }),
        ];
      })),
    );
    written.push(
      ...(await observe(w, async () => {
        await w.t.mutation(internal.clerkWebhook.applyEvent, {
          eventId: "audit-member-role",
          event: {
            ...member,
            occurredAt: NOW + 2,
            updatedAt: NOW + 2,
            role: "gestionnaire",
          },
        });
        return [
          system(
            "membership.role_changed",
            memberId,
            "user",
            "webhook",
            w.orgA,
            { role: "gestionnaire", previousRole: "user" },
          ),
        ];
      })),
    );
    const deactivation = {
      eventId: "audit-member-delete",
      event: {
        ...member,
        role: "gestionnaire",
        deleted: true,
        occurredAt: NOW + 3,
        updatedAt: NOW + 3,
      },
    } as const;
    written.push(
      ...(await observe(w, async () => {
        await w.t.mutation(internal.clerkWebhook.applyEvent, deactivation);
        return [
          system("membership.deactivated", memberId, "user", "webhook", w.orgA),
        ];
      })),
    );
    await unchanged(w, () =>
      w.t.mutation(internal.clerkWebhook.applyEvent, deactivation),
    );
    await w.t.run((ctx) =>
      ctx.db.insert("memberships", {
        userId: w.patient,
        organizationId: w.orgB,
        role: "user",
        active: true,
      }),
    );
    written.push(
      ...(await observe(w, async () => {
        await w.t.mutation(internal.clerkWebhook.applyEvent, {
          eventId: "audit-account-delete",
          event: {
            kind: "userDeleted",
            userId: "patient",
            occurredAt: NOW + 4,
            updatedAt: NOW + 4,
          },
        });
        return [w.orgA, w.orgB].map((org) =>
          system("account.deleted_in_clerk", w.patient, "user", "webhook", org),
        );
      })),
    );
    const count = await w.t.run(
      async (ctx) =>
        (await ctx.db.query("memberships").collect()).filter(
          (row) => row.organizationId === w.orgA && row.active,
        ).length,
    );
    written.push(
      ...(await observe(w, async () => {
        await w.t.mutation(internal.clerkWebhook.applyEvent, {
          eventId: "audit-org-delete",
          event: {
            kind: "organization",
            deleted: true,
            organization,
            occurredAt: NOW + 5,
            updatedAt: NOW + 5,
          },
        });
        return [
          system(
            "organization.deleted",
            w.orgA,
            "organization",
            "webhook",
            w.orgA,
            { count },
          ),
        ];
      })),
    );
    return written;
  },
  "users.getOrCreateUser": async (w) => {
    const identity = w.t.withIdentity({
      subject: "synthetic-c1-login",
      org_id: ORG_A_CLERK_ORG,
      org_role: "org:patient",
    });
    let userId: Id<"users">;
    const written = await observe(w, async () => {
      userId = await identity.mutation(api.users.getOrCreateUser, {});
      return [
        {
          ...site(w, "membership.created", userId, "user", "patient", w.orgA, {
            role: "user",
          }),
          actorId: userId,
        },
      ];
    });
    written.push(
      ...(await observe(w, async () => {
        await w.t
          .withIdentity({
            subject: "synthetic-c1-login",
            org_id: ORG_A_CLERK_ORG,
            org_role: "org:admin",
          })
          .mutation(api.users.getOrCreateUser, {});
        return [
          {
            action: "membership.role_changed",
            targetType: "user",
            targetId: userId,
            source: "site",
            actorId: userId,
            actorRole: "org_admin",
            organizationId: w.orgA,
            details: { role: "admin", previousRole: "user" },
          },
        ];
      })),
    );
    return written;
  },
  "fitness.submitFitnessAnswers": async (w) =>
    observe(w, async () => {
      const assessment = await as(w.t, "patient").mutation(
        api.fitness.submitFitnessAnswers,
        {
          questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
          answers: Object.fromEntries(
            CURRENT_QUESTIONNAIRE.questions.map((q) => [
              q.id,
              q.outcomes.no !== "fit",
            ]),
          ),
        },
      );
      return [
        site(
          w,
          "fitness.submitted",
          assessment.assessmentId,
          "fitness_assessment",
          "patient",
          w.orgA,
          { userId: w.patient },
        ),
      ];
    }),
  "recordStorage.enforceRetention": async (w) => {
    vi.stubEnv("RECORD_RETENTION_DAYS", "1");
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(sessionId, {
        record: "complete",
        endedAt: NOW - 2 * 86400000,
      }),
    );
    const foreignSession = await addSession(w, {
      machineId: w.orgBMachine,
      userId: w.orgBPatient,
      status: "completed",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(foreignSession, {
        record: "complete",
        endedAt: NOW - 2 * 86400000,
      }),
    );
    const written = await observe(w, async () => {
      await w.t.mutation(internal.recordStorage.enforceRetention, {
        cursor: null,
      });
      return [
        system("record.deleted", sessionId, "session", "cron", w.orgA),
        system("record.deleted", foreignSession, "session", "cron", w.orgB),
      ];
    });
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      record: "none",
      recordDeleted: true,
    });
    await unchanged(w, () =>
      w.t.mutation(internal.recordStorage.enforceRetention, { cursor: null }),
    );
    return written;
  },
  "fitness.validateFitnessAssessment": async (w) => {
    const answers = Object.fromEntries(
      CURRENT_QUESTIONNAIRE.questions.map((q) => [
        q.id,
        q.outcomes.no !== "fit",
      ]),
    );
    const assessment = await as(w.t, "patient").mutation(
      api.fitness.submitFitnessAnswers,
      { questionnaireVersion: CURRENT_QUESTIONNAIRE.version, answers },
    );
    return observe(w, async () => {
      await as(w.t, "manager").mutation(api.fitness.validateFitnessAssessment, {
        assessmentId: assessment.assessmentId,
        validUntil: NOW + 86400000,
        notes: "synthetic-private-fitness-notes",
      });
      return [
        site(
          w,
          "fitness.validated",
          assessment.assessmentId,
          "fitness_assessment",
          "manager",
          w.orgA,
          { userId: w.patient },
        ),
      ];
    });
  },
  "fitness.signConsent": async (w) =>
    observe(w, async () => {
      const consentId = await as(w.t, "orgAdmin").mutation(
        api.fitness.signConsent,
        { textVersion: CONSENT_TEXT_VERSION },
      );
      return [site(w, "consent.signed", consentId, "consent", "orgAdmin")];
    }),
  "fitness.withdrawConsent": async (w) => {
    const consentIds = await w.t.run(async (ctx) => {
      const prior = await ctx.db
        .query("consents")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.patient).eq("organizationId", w.orgA),
        )
        .collect();
      const duplicate = await ctx.db.insert("consents", {
        userId: w.patient,
        organizationId: w.orgA,
        textVersion: CONSENT_TEXT_VERSION,
        signedAt: NOW,
        signedVia: "site",
      });
      return [...prior.map((row) => row._id), duplicate];
    });
    return observe(w, async () => {
      await as(w.t, "patient").mutation(api.fitness.withdrawConsent, {});
      return consentIds.map((id) =>
        site(w, "consent.withdrawn", id, "consent", "patient"),
      );
    });
  },
  "machineKeys.createMachineKey": async (w) => {
    const old = await key(w);
    return observe(w, async () => {
      const issued = await as(w.t, "orgAdmin").mutation(
        api.machineKeys.createMachineKey,
        { machineId: w.machine, overlapMinutes: 0, lifetimeMinutes: 60 },
      );
      return [
        site(
          w,
          "machine_key.created",
          issued.keyId,
          "machine_key",
          "orgAdmin",
          w.orgA,
          { machineId: w.machine, count: 1, expiresAt: NOW + 3600000 },
        ),
        site(w, "machine_key.revoked", old.keyId, "machine_key", "orgAdmin"),
      ];
    });
  },
  "machineKeys.revokeMachineKey": async (w) => {
    const issued = await key(w);
    return observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(api.machineKeys.revokeMachineKey, {
        keyId: issued.keyId,
      });
      return [
        site(w, "machine_key.revoked", issued.keyId, "machine_key", "orgAdmin"),
      ];
    });
  },
  "machineKeys.revokeExpiredKeys": async (w) => {
    const issued = await key(w);
    await w.t.run((ctx) => ctx.db.patch(issued.keyId, { expiresAt: NOW }));
    return observe(w, async () => {
      expect(
        await w.t.mutation(internal.machineKeys.revokeExpiredKeys, {}),
      ).toEqual({ revoked: 1 });
      return [
        system(
          "machine_key.expired_revoked",
          issued.keyId,
          "machine_key",
          "cron",
          w.orgA,
        ),
      ];
    });
  },
  "machineKeys.createKeyByOperator": async (w) => {
    const old = await key(w);
    return observe(w, async () => {
      const issued = await w.t.mutation(
        internal.machineKeys.createKeyByOperator,
        { machineId: w.machine, overlapMinutes: 0 },
      );
      return [
        system(
          "machine_key.created",
          issued.keyId,
          "machine_key",
          "operator",
          w.orgA,
          { machineId: w.machine, count: 1 },
        ),
        system(
          "machine_key.revoked",
          old.keyId,
          "machine_key",
          "operator",
          w.orgA,
        ),
      ];
    });
  },
  "machineKeys.revokeKeyByOperator": async (w) => {
    const issued = await key(w);
    return observe(w, async () => {
      await w.t.mutation(internal.machineKeys.revokeKeyByOperator, {
        keyId: issued.keyId,
      });
      return [
        system(
          "machine_key.revoked",
          issued.keyId,
          "machine_key",
          "operator",
          w.orgA,
        ),
      ];
    });
  },
  "machineRegistry.updateIdentity": async (w) =>
    observe(w, async () => {
      await as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
        machineId: w.machine,
        site: "synthetic-private-postal-address",
      });
      return [
        site(
          w,
          "registry.identity_updated",
          w.machine,
          "machine",
          "admin",
          w.orgA,
          { fields: ["site"] },
        ),
      ];
    }),
  "machineRegistry.recordRadii": async (w) =>
    observe(w, async () => {
      await as(w.t, "admin").mutation(api.machineRegistry.recordRadii, {
        machineId: w.machine,
        armRadiusM: 1.2,
        legTipRadiusM: 1.5,
        measuredAt: NOW,
        measuredBy: "Synthetic measurer",
      });
      return [site(w, "registry.radii_recorded", w.machine, "machine")];
    }),
  "machineRegistry.attachDriveParameterSheet": async (w) => {
    const storageId = await registryUpload(w);
    return observe(w, async () => {
      await as(w.t, "admin").mutation(
        api.machineRegistry.attachDriveParameterSheet,
        {
          machineId: w.machine,
          storageId,
          version: "1.0.0",
          signedBy: "Synthetic signer",
          at: NOW,
        },
      );
      return [
        site(
          w,
          "registry.drive_sheet_attached",
          w.machine,
          "machine",
          "admin",
          w.orgA,
        ),
      ];
    });
  },
  "machineRegistry.addValidationReview": async (w) => {
    await registryRelease(w);
    const reportStorageId = await registryUpload(w);
    return observe(w, async () => {
      await as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
        machineId: w.machine,
        level: "bench",
        softwareVersion: "pi-0.1.0",
        medicalParametersVersion: null,
        reportStorageId,
      });
      return [
        site(
          w,
          "registry.review_added",
          w.machine,
          "machine",
          "admin",
          w.orgA,
          {
            level: "bench",
            previousLevel: "none",
            softwareVersion: "pi-0.1.0",
          },
        ),
      ];
    });
  },
  "machineRegistry.lowerValidationLevel": async (w) => {
    await benchReview(w);
    return observe(w, async () => {
      await as(w.t, "admin").mutation(
        api.machineRegistry.lowerValidationLevel,
        {
          machineId: w.machine,
          level: "none",
          reason: "synthetic-private-downgrade-reason",
        },
      );
      return [
        site(
          w,
          "registry.level_lowered",
          w.machine,
          "machine",
          "admin",
          w.orgA,
          { level: "none", previousLevel: "bench" },
        ),
      ];
    });
  },
  "machines.createMachine": async (w) =>
    observe(w, async () => {
      const created = await as(w.t, "admin").mutation(
        api.machines.createMachine,
        {
          name: "Synthetic audit machine",
          organizationId: w.orgA,
          gestionnaireIds: [w.manager],
        },
      );
      const createdKey = await w.t.run((ctx) =>
        ctx.db
          .query("machine_api_keys")
          .withIndex("by_machine", (q) => q.eq("machineId", created.machineId))
          .unique(),
      );
      if (!createdKey) throw new Error("Missing first machine key");
      return [
        site(w, "machine.created", created.machineId, "machine"),
        site(w, "machine_key.created", createdKey._id, "machine_key"),
        site(
          w,
          "machine_gestionnaires.set",
          created.machineId,
          "machine",
          "admin",
          w.orgA,
          { added: [w.manager] },
        ),
      ];
    }),
  "machines.assignMachineToGestionnaires": async (w) =>
    observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(
        api.machines.assignMachineToGestionnaires,
        { machineId: w.machine, gestionnaireIds: [w.otherManager] },
      );
      return [
        site(
          w,
          "machine_gestionnaires.set",
          w.machine,
          "machine",
          "orgAdmin",
          w.orgA,
          { added: [w.otherManager], removed: [w.manager] },
        ),
      ];
    }),
  "machines.setGestionnaireMachines": async (w) =>
    observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(api.machines.setGestionnaireMachines, {
        gestionnaireId: w.manager,
        machineIds: [w.otherMachine],
      });
      return [
        site(
          w,
          "gestionnaire_machines.set",
          w.manager,
          "user",
          "orgAdmin",
          w.orgA,
          { added: [w.otherMachine], removed: [w.machine] },
        ),
      ];
    }),
  "machines.deleteMachine": async (w) =>
    observe(w, async () => {
      await as(w.t, "manager").mutation(api.machines.deleteMachine, {
        machineId: w.machine,
      });
      return [site(w, "machine.deleted", w.machine, "machine", "manager")];
    }),
  "machines.restoreMachine": async (w) => {
    await as(w.t, "manager").mutation(api.machines.deleteMachine, {
      machineId: w.machine,
    });
    return observe(w, async () => {
      await as(w.t, "admin").mutation(api.machines.restoreMachine, {
        machineId: w.machine,
      });
      return [site(w, "machine.restored", w.machine, "machine")];
    });
  },
  "machines.assignGestionnaireToMachine": async (w) =>
    observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(
        api.machines.assignGestionnaireToMachine,
        { machineId: w.machine, gestionnaireId: w.otherManager },
      );
      return [
        site(
          w,
          "machine_gestionnaire.assigned",
          w.machine,
          "machine",
          "orgAdmin",
          w.orgA,
          { gestionnaireId: w.otherManager },
        ),
      ];
    }),
  "machines.removeGestionnaireFromMachine": async (w) =>
    observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(
        api.machines.removeGestionnaireFromMachine,
        { machineId: w.machine, gestionnaireId: w.manager },
      );
      return [
        site(
          w,
          "machine_gestionnaire.removed",
          w.machine,
          "machine",
          "orgAdmin",
          w.orgA,
          { gestionnaireId: w.manager },
        ),
      ];
    }),
  "medicalParametersStore.insertVersion": async () => {
    const t = backendOf(modules);
    return observe({ t }, async () => {
      await registerShippedMedicalParameters(t);
      const version = await t.run((ctx) =>
        ctx.db.query("medical_parameters_versions").unique(),
      );
      if (!version) throw new Error("Missing medical version");
      return [
        system(
          "medical_parameters.registered",
          version._id,
          "medical_parameters_version",
          "operator",
          undefined,
        ),
      ];
    });
  },
  "patientInvitations.prepare": async (w) => {
    mockClerkInvitations();
    return observe(w, async () => {
      const sent = await as(w.t, "manager").action(api.users.createPatient, {
        firstName: "Synthetic",
        lastName: "Invitee",
        email: "synthetic-audit-invitee@example.invalid",
      });
      const invitation = await w.t.run((ctx) => ctx.db.get(sent.invitationId));
      expect(invitation?.userId).toBeDefined();
      return [
        site(
          w,
          "user.invited",
          sent.invitationId,
          "patient_invitation",
          "manager",
          w.orgA,
          { role: "user", userId: invitation?.userId, added: [w.manager] },
        ),
      ];
    });
  },
  "recordStorage.remove": async (w) => {
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
    });
    return observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(api.recordStorage.remove, {
        sessionId,
      });
      return [site(w, "record.deleted", sessionId, "session", "orgAdmin")];
    });
  },
  "softwareReleases.recordRelease": async (w) =>
    observe(w, async () => {
      const releaseId = await as(w.t, "admin").mutation(
        api.softwareReleases.recordRelease,
        {
          component: "pi",
          version: "pi-1.2.3",
          releasedAt: NOW,
          validationLevel: "bench",
        },
      );
      return [
        site(
          w,
          "software_release.recorded",
          releaseId,
          "software_release",
          "admin",
          w.anheartOrg,
          { version: "pi-1.2.3", level: "bench" },
        ),
      ];
    }),
  "training.grantLaunchRight": async (w) => {
    await as(w.t, "manager").mutation(api.training.revokeLaunchRight, {
      machineId: w.machine,
      userId: w.patient,
    });
    return observe(w, async () => {
      await as(w.t, "manager").mutation(api.training.grantLaunchRight, {
        machineId: w.machine,
        userId: w.patient,
      });
      return [
        site(w, "launch_right.granted", w.patient, "user", "manager", w.orgA, {
          machineId: w.machine,
        }),
      ];
    });
  },
  "training.revokeLaunchRight": async (w) =>
    observe(w, async () => {
      await as(w.t, "manager").mutation(api.training.revokeLaunchRight, {
        machineId: w.machine,
        userId: w.patient,
      });
      return [
        site(w, "launch_right.revoked", w.patient, "user", "manager", w.orgA, {
          machineId: w.machine,
        }),
      ];
    }),
  "training.setUserPhysiology": async (w) =>
    observe(w, async () => {
      await as(w.t, "manager").mutation(api.training.setUserPhysiology, {
        userId: w.patient,
        hrMax: 187,
        birthYear: 1990,
      });
      return [
        site(w, "physiology.updated", w.patient, "user", "manager", w.orgA, {
          fields: ["hrMax"],
        }),
      ];
    }),
  "training.launchAutoSession": async (w) =>
    observe(w, async () => {
      const sessionId = await as(w.t, "manager").mutation(
        api.training.launchAutoSession,
        { machineId: w.machine, userId: w.patient, profileId: w.profileId },
      );
      return [
        site(w, "session.launched", sessionId, "session", "manager", w.orgA, {
          machineId: w.machine,
          userId: w.patient,
        }),
      ];
    }),
  "training.requestStop": async (w) => {
    const pending = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
      kind: "auto",
    });
    const active = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    return observe(w, async () => {
      for (const sessionId of [pending, active])
        await as(w.t, "patient").mutation(api.training.requestStop, {
          sessionId,
        });
      return [
        site(w, "session.cancelled", pending, "session", "patient"),
        site(w, "session.stop_requested", active, "session", "patient"),
      ];
    });
  },
  "training.markTrainingStarted": async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await w.admin.mutation(api.training.launchAutoSession, {
      machineId: w.machine,
      userId: w.patient,
      profileId: w.profileId,
    });
    return observe(w, async () => {
      expect((await machinePost(w, "start", { sessionId })).status).toBe(200);
      return [
        {
          action: "session.started",
          targetId: sessionId,
          targetType: "session",
          source: "machine",
          actorRole: "machine",
          actorId: w.machine,
          organizationId: w.organizationId,
        },
      ];
    });
  },
  "training.registerLocalSession": async () => {
    const w = await seedMachineWorld(modules);
    return observe(w, async () => {
      const response = await machinePost(w, "local", {
        localRef: "synthetic-audit-completeness",
        kind: "manual",
        startedAt: NOW,
        operatorName: "Synthetic operator",
      });
      expect(response.status).toBe(200);
      const { sessionId } = (await response.json()) as { sessionId: string };
      return [
        {
          action: "session.local_registered",
          targetId: sessionId,
          targetType: "session",
          source: "machine",
          actorRole: "machine",
          actorId: w.machine,
          organizationId: w.organizationId,
        },
      ];
    });
  },
  "training.endTrainingSession": async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    return observe(w, async () => {
      expect(
        (
          await machinePost(w, "end", {
            sessionId,
            failed: false,
            reason: "synthetic-private-end-code-and-reason",
          })
        ).status,
      ).toBe(200);
      return [
        {
          action: "session.ended",
          targetId: sessionId,
          targetType: "session",
          source: "machine",
          actorRole: "machine",
          actorId: w.machine,
          organizationId: w.organizationId,
          details: { outcome: "completed" },
        },
      ];
    });
  },
  "users.updateUserRole": async (w) => {
    configureAnheartOrganization(null);
    await w.t.run((ctx) =>
      ctx.db.patch(w.anheartOrg, { clerkOrgId: undefined }),
    );
    return observe(w, async () => {
      await w.t
        .withIdentity({ subject: "admin" })
        .mutation(api.users.updateUserRole, {
          userId: w.admin,
          role: "gestionnaire",
        });
      return [
        site(w, "user.role_changed", w.admin, "user", "admin", w.anheartOrg, {
          role: "gestionnaire",
          previousRole: "admin",
        }),
      ];
    });
  },
  "users.deleteUser": async (w) => {
    await w.t.run((ctx) =>
      ctx.db.insert("memberships", {
        userId: w.patient,
        organizationId: w.orgB,
        role: "user",
        active: true,
      }),
    );
    return observe(w, async () => {
      await as(w.t, "admin").mutation(api.users.deleteUser, {
        userId: w.patient,
      });
      return [w.orgA, w.orgB].map((org) =>
        site(w, "user.deleted", w.patient, "user", "admin", org),
      );
    });
  },
  "users.linkPatientToClerk": async (w) => {
    await w.t.run((ctx) => ctx.db.patch(w.patient, { clerkId: "" }));
    return observe(w, async () => {
      expect(
        await as(w.t, "patient", {
          email: "patient@example.invalid",
          emailVerified: true,
        }).mutation(api.users.linkPatientToClerk, {
          email: "patient@example.invalid",
        }),
      ).toBe(w.patient);
      return [site(w, "user.linked_to_account", w.patient, "user", "patient")];
    });
  },
  "users.assignGestionnaireToUser": async (w) =>
    observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(api.users.assignGestionnaireToUser, {
        userId: w.patient,
        gestionnaireId: w.otherManager,
      });
      return [
        site(
          w,
          "patient_gestionnaire.assigned",
          w.patient,
          "user",
          "orgAdmin",
          w.orgA,
          { gestionnaireId: w.otherManager },
        ),
      ];
    }),
  "users.removeGestionnaireFromUser": async (w) => {
    await w.t.run((ctx) =>
      ctx.db.insert("user_gestionnaires", {
        userId: w.patient,
        gestionnaireId: w.manager,
        organizationId: w.orgB,
        createdAt: NOW,
        createdBy: w.admin,
      }),
    );
    return observe(w, async () => {
      await as(w.t, "admin").mutation(api.users.removeGestionnaireFromUser, {
        userId: w.patient,
        gestionnaireId: w.manager,
      });
      return [w.orgA, w.orgB].map((org) =>
        site(
          w,
          "patient_gestionnaire.removed",
          w.patient,
          "user",
          "admin",
          org,
          { gestionnaireId: w.manager },
        ),
      );
    });
  },
  "users.assignPatientsToGestionnaire": async (w) =>
    observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(
        api.users.assignPatientsToGestionnaire,
        { gestionnaireId: w.manager, patientIds: [w.otherPatient] },
      );
      return [
        site(
          w,
          "gestionnaire_patients.set",
          w.manager,
          "user",
          "orgAdmin",
          w.orgA,
          { added: [w.otherPatient], removed: [w.patient] },
        ),
      ];
    }),
};

describe("ANH-167 T02-a completeness through registered entry points", () => {
  const observedActions = new Set<AuditAction>();
  it("has exactly the sensitive mutation keys", () => {
    expect(Object.keys(SCENARIOS).sort()).toEqual(
      Object.keys(SENSITIVE_MUTATIONS).sort(),
    );
  });
  it.each(Object.entries(SCENARIOS))(
    "records %s with complete request metadata",
    async (name, scenario) => {
      const written = await scenario(await seedLaunchWorld(modules));
      expect(written.length).toBeGreaterThan(0);
      for (const action of SENSITIVE_MUTATIONS[name])
        expect(written.map((row) => row.action)).toContain(action);
      for (const row of written) {
        observedActions.add(row.action);
        expect(row.requestId.length).toBeGreaterThan(0);
        for (const privateValue of [
          "synthetic-private-fitness-notes",
          "synthetic-private-postal-address",
          "synthetic-private-downgrade-reason",
          "synthetic-private-end-code-and-reason",
        ])
          expect(JSON.stringify(row)).not.toContain(privateValue);
      }
      if (name === "machines.createMachine")
        expect(new Set(written.map((row) => row.requestId)).size).toBe(1);
    },
  );
  it("observes every closed action in a sensitive mutation scenario", () => {
    expect([...observedActions].sort()).toEqual([...AUDIT_ACTIONS].sort());
  });
});

describe("ANH-167 T02 training safety and absent effects", () => {
  it("keeps grant/revoke replays unwritten and records only the changed launch right", async () => {
    const w = await seedLaunchWorld(modules);
    const args = { machineId: w.machine, userId: w.patient };
    const manager = as(w.t, "manager");
    await unchanged(w, () =>
      manager.mutation(api.training.grantLaunchRight, args),
    );
    await manager.mutation(api.training.revokeLaunchRight, args);
    expectEntry((await rows(w)).slice(-1)[0], {
      action: "launch_right.revoked",
      source: "site",
      actorRole: "gestionnaire",
      actorId: w.manager,
      organizationId: w.orgA,
      targetType: "user",
      targetId: w.patient,
      details: { machineId: w.machine },
    });
    await unchanged(w, () =>
      manager.mutation(api.training.revokeLaunchRight, args),
    );
    await manager.mutation(api.training.grantLaunchRight, args);
    expect((await rows(w)).slice(-1)[0]?.action).toBe("launch_right.granted");
  });

  it("always stops active sessions and cancels pending sessions with one row per effect", async () => {
    const w = await seedLaunchWorld(modules);
    for (const status of ["active", "pending", "completed"] as const) {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status,
        kind: "auto",
      });
      const call = () =>
        as(w.t, "patient").mutation(api.training.requestStop, { sessionId });
      if (status === "completed") {
        await unchanged(w, call);
        continue;
      }
      const before = (await rows(w)).length;
      await call();
      const session = await w.t.run((ctx) => ctx.db.get(sessionId));
      if (status === "active") expect(session?.stopRequestedAt).toBe(NOW);
      else expect(session?.status).toBe("failed");
      expect((await rows(w)).length).toBe(before + 1);
      expectEntry((await rows(w)).slice(-1)[0], {
        action:
          status === "active" ? "session.stop_requested" : "session.cancelled",
        source: "site",
        actorRole: "user",
        actorId: w.patient,
        organizationId: w.orgA,
        targetType: "session",
        targetId: sessionId,
      });
      await unchanged(w, call);
    }
  });

  it("omits physiology values and refuses a patient without an audit row", async () => {
    const w = await seedLaunchWorld(modules);
    const args = { userId: w.patient, hrMax: 187, birthYear: 1990 };
    await unchanged(w, () =>
      expect(
        as(w.t, "patient").mutation(api.training.setUserPhysiology, args),
      ).rejects.toThrow("not_authorized"),
    );
    await as(w.t, "manager").mutation(api.training.setUserPhysiology, args);
    const row = (await rows(w)).slice(-1)[0];
    expect(row?.details?.fields).toEqual(["hrMax"]);
    expect(JSON.stringify(row)).not.toContain("187");
    expect(JSON.stringify(row)).not.toContain("1990");
  });

  it("commits arming refusal as failed, keeps HTTP 409, and writes no started row on refusal or replay", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await w.admin.mutation(api.training.launchAutoSession, {
      machineId: w.machine,
      userId: w.patient,
      profileId: w.profileId,
    });
    await w.t.run((ctx) => ctx.db.patch(w.patient, { hrMax: 187 }));
    const call = async () => {
      const response = await machinePost(w, "start", { sessionId });
      expect(response.status).toBe(409);
      expect(await response.json()).toMatchObject({
        error: "hr_max_changed_before_start",
      });
      expect((await w.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe(
        "failed",
      );
    };
    await unchanged(w, call);
    await unchanged(w, call);
  });

  it("records local and end effects once through HTTP and omits the end reason", async () => {
    const w = await seedMachineWorld(modules);
    const body = {
      localRef: "synthetic-audit-local",
      kind: "manual",
      startedAt: NOW,
      operatorName: "Synthetic operator",
    };
    const first = await machinePost(w, "local", body);
    expect(first.status).toBe(200);
    const { sessionId } = (await first.json()) as { sessionId: string };
    expectEntry((await rows(w)).slice(-1)[0], {
      action: "session.local_registered",
      source: "machine",
      actorRole: "machine",
      actorId: w.machine,
      organizationId: w.organizationId,
      targetType: "session",
      targetId: sessionId,
    });
    await unchanged(w, async () =>
      expect((await machinePost(w, "local", body)).status).toBe(200),
    );
    const end = {
      sessionId,
      failed: true,
      reason: "synthetic-private-end-code-and-reason",
    };
    expect((await machinePost(w, "end", end)).status).toBe(200);
    const row = (await rows(w)).slice(-1)[0];
    expectEntry(row, {
      action: "session.ended",
      source: "machine",
      actorRole: "machine",
      actorId: w.machine,
      organizationId: w.organizationId,
      targetType: "session",
      targetId: sessionId,
      details: { outcome: "failed" },
    });
    expect(JSON.stringify(row)).not.toContain(end.reason);
    await unchanged(w, async () =>
      expect((await machinePost(w, "end", end)).status).toBe(200),
    );
  });
});

describe("ANH-167 T02-b absent effects and refusals", () => {
  it("leaves consent, revoked keys and unchanged relationship sets unwritten", async () => {
    const w = await seedLaunchWorld(modules);
    await unchanged(w, () =>
      as(w.t, "patient").mutation(api.fitness.signConsent, {
        textVersion: CONSENT_TEXT_VERSION,
      }),
    );
    const issued = await key(w);
    await as(w.t, "orgAdmin").mutation(api.machineKeys.revokeMachineKey, {
      keyId: issued.keyId,
    });
    await unchanged(w, () =>
      as(w.t, "orgAdmin").mutation(api.machineKeys.revokeMachineKey, {
        keyId: issued.keyId,
      }),
    );
    await unchanged(w, () =>
      w.t.mutation(internal.machineKeys.revokeKeyByOperator, {
        keyId: issued.keyId,
      }),
    );
    await unchanged(w, () =>
      w.t.mutation(internal.machineKeys.revokeExpiredKeys, {}),
    );
    await unchanged(w, () =>
      as(w.t, "orgAdmin").mutation(api.machines.assignMachineToGestionnaires, {
        machineId: w.machine,
        gestionnaireIds: [w.manager],
      }),
    );
    await unchanged(w, () =>
      as(w.t, "orgAdmin").mutation(api.machines.setGestionnaireMachines, {
        gestionnaireId: w.manager,
        machineIds: [w.machine],
      }),
    );
    await unchanged(w, () =>
      as(w.t, "orgAdmin").mutation(api.users.assignPatientsToGestionnaire, {
        gestionnaireId: w.manager,
        patientIds: [w.patient],
      }),
    );
  });

  it("leaves three not_authorized calls unwritten", async () => {
    const w = await seedLaunchWorld(modules);
    const issued = await key(w);
    const patient = as(w.t, "patient");
    for (const call of [
      () =>
        patient.mutation(api.machines.createMachine, {
          name: "Synthetic refused machine",
        }),
      () =>
        patient.mutation(api.machineKeys.revokeMachineKey, {
          keyId: issued.keyId,
        }),
      () =>
        patient.mutation(api.users.assignGestionnaireToUser, {
          userId: w.patient,
          gestionnaireId: w.otherManager,
        }),
    ])
      await unchanged(w, () =>
        expect(call()).rejects.toThrow("not_authorized"),
      );
  });

  it("audits an ambiguous invitation without a profile and leaves its replay unwritten", async () => {
    const w = await seedLaunchWorld(modules);
    mockClerkInvitations();
    const email = "synthetic-ambiguous@example.invalid";
    await w.t.run(async (ctx) => {
      for (const userId of [w.patient, w.orgBPatient])
        await ctx.db.patch(userId, { email });
    });
    const input = { email, firstName: "Synthetic", lastName: "Ambiguous" };
    const written = await observe(w, async () => {
      const invitation = await as(w.t, "manager").action(
        api.users.createPatient,
        input,
      );
      expect(
        (await w.t.run((ctx) => ctx.db.get(invitation.invitationId)))?.userId,
      ).toBeUndefined();
      return [
        site(
          w,
          "user.invited",
          invitation.invitationId,
          "patient_invitation",
          "manager",
          w.orgA,
          { role: "user" },
        ),
      ];
    });
    expect(written[0].details).toEqual({ role: "user", added: [w.manager] });
    await unchanged(w, () =>
      as(w.t, "manager").action(api.users.createPatient, input),
    );
  });
});

describe("ANH-167 C1 regression effects", () => {
  it("records only creation when reactivation also changes the membership role", async () => {
    const w = await seedLaunchWorld(modules);
    const event = membershipEvent();
    await w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId: "c1-inactive",
      event: { ...event, deleted: true },
    });
    const member = await w.t.run(async (ctx) =>
      (await ctx.db.query("memberships").collect()).find(
        (row) => row.clerkMembershipId === event.membershipId,
      ),
    );
    expect(member?.active).toBe(false);
    await observe(w, async () => {
      await w.t.mutation(internal.clerkWebhook.applyEvent, {
        eventId: "c1-reactivation-role",
        event: {
          ...event,
          membershipId: "synthetic-reactivated",
          role: "admin",
          updatedAt: NOW + 2,
          occurredAt: NOW + 2,
        },
      });
      return [
        system(
          "membership.created",
          member!.userId,
          "user",
          "webhook",
          w.orgA,
          { role: "admin" },
        ),
      ];
    });
  });

  it("records a submission that removes previously validated launch fitness without answers or verdict", async () => {
    const w = await seedLaunchWorld(modules);
    const written = await SCENARIOS["fitness.submitFitnessAnswers"](w);
    expect(written[0].details).toEqual({ userId: w.patient });
    await unchanged(w, () =>
      expect(
        as(w.t, "manager").mutation(api.training.launchAutoSession, {
          machineId: w.machine,
          userId: w.patient,
          profileId: w.profileId,
        }),
      ).rejects.toThrow("rider_not_fit"),
    );
  });

  it.each(["user", "gestionnaire", "admin"] as const)(
    "R2-1 P2 restores an inactive %s membership exactly once",
    async (role) => {
      const w = await seedLaunchWorld(modules);
      const event = membershipEvent(role);
      await w.t.mutation(internal.clerkWebhook.applyEvent, {
        eventId: "c1-created",
        event,
      });
      await w.t.mutation(internal.clerkWebhook.applyEvent, {
        eventId: "c1-deleted",
        event: {
          ...event,
          deleted: true,
          occurredAt: NOW + 2,
          updatedAt: NOW + 2,
        },
      });
      const restored = {
        ...event,
        membershipId: "synthetic-new-membership",
        occurredAt: NOW + 3,
        updatedAt: NOW + 3,
      };
      await observe(w, async () => {
        await w.t.mutation(internal.clerkWebhook.applyEvent, {
          eventId: "c1-restored",
          event: restored,
        });
        const membership = await w.t.run(async (ctx) =>
          (await ctx.db.query("memberships").collect()).find(
            (row) => row.clerkMembershipId === restored.membershipId,
          ),
        );
        expect(membership?.active).toBe(true);
        return [
          system(
            "membership.created",
            membership!.userId,
            "user",
            "webhook",
            w.orgA,
            { role },
          ),
        ];
      });
      await unchanged(w, async () => {
        await w.t.mutation(internal.clerkWebhook.applyEvent, {
          eventId: "c1-restored",
          event: restored,
        });
        await w.t.mutation(internal.clerkWebhook.applyEvent, {
          eventId: "c1-older",
          event: { ...event, role: "user" },
        });
      });
    },
  );

  it("P5 audits activation, account linking and each assignment on invitation acceptance", async () => {
    await acceptInvitation(await seedLaunchWorld(modules));
  });

  it.each(["mirror-first", "webhook-first"] as const)(
    "R2-1b P1 traces one creation for %s",
    async (order) => {
      const w = await seedLaunchWorld(modules);
      const before = await rows(w);
      const event = membershipEvent("admin");
      const mirror = () =>
        w.t
          .withIdentity({
            subject: event.userId,
            org_id: ORG_A_CLERK_ORG,
            org_role: "org:admin",
          })
          .mutation(api.users.getOrCreateUser, {});
      const webhook = () =>
        w.t.mutation(internal.clerkWebhook.applyEvent, {
          eventId: "c1-order",
          event,
        });
      if (order === "mirror-first") {
        await mirror();
        await webhook();
      } else {
        await webhook();
        await mirror();
      }
      const written = (await rows(w)).filter(
        (row) => row.action === "membership.created",
      );
      expect(written).toHaveLength(1);
      const userId = await mirror();
      expectEntry(
        written[0],
        order === "mirror-first"
          ? {
              ...site(
                w,
                "membership.created",
                userId,
                "user",
                "orgAdmin",
                w.orgA,
                { role: "admin" },
              ),
              actorId: userId,
            }
          : system("membership.created", userId, "user", "webhook", w.orgA, {
              role: "admin",
            }),
      );
      expect(await rows(w)).toEqual([...before, ...written]);
    },
  );

  it("P1 audits insertion and role changes of an existing account mirror, including a changed Clerk id", async () => {
    const w = await seedLaunchWorld(modules);
    const mirror = (org_role: string) =>
      w.t
        .withIdentity({ subject: "patient", org_id: ORG_B_CLERK_ORG, org_role })
        .mutation(api.users.getOrCreateUser, {});
    await observe(w, async () => {
      await mirror("org:patient");
      return [
        {
          ...site(
            w,
            "membership.created",
            w.patient,
            "user",
            "patient",
            w.orgB,
            { role: "user" },
          ),
          actorId: w.patient,
        },
      ];
    });
    await observe(w, async () => {
      await mirror("org:admin");
      return [
        {
          ...site(
            w,
            "membership.role_changed",
            w.patient,
            "user",
            "orgAdmin",
            w.orgB,
            { role: "admin", previousRole: "user" },
          ),
          actorId: w.patient,
        },
      ];
    });
    const event = {
      ...membershipEvent("gestionnaire"),
      userId: "patient",
      organization: { ...membershipEvent().organization, id: ORG_B_CLERK_ORG },
    };
    await observe(w, async () => {
      await w.t.mutation(internal.clerkWebhook.applyEvent, {
        eventId: "c1-mirror-role",
        event,
      });
      return [
        system(
          "membership.role_changed",
          w.patient,
          "user",
          "webhook",
          w.orgB,
          { role: "gestionnaire", previousRole: "admin" },
        ),
      ];
    });
    await unchanged(w, () => mirror("org:patient"));
    await observe(w, async () => {
      await w.t.mutation(internal.clerkWebhook.applyEvent, {
        eventId: "c1-new-clerk-id",
        event: {
          ...event,
          membershipId: "synthetic-other-id",
          role: "admin",
          occurredAt: NOW + 2,
          updatedAt: NOW + 2,
        },
      });
      return [
        system(
          "membership.role_changed",
          w.patient,
          "user",
          "webhook",
          w.orgB,
          { role: "admin", previousRole: "gestionnaire" },
        ),
      ];
    });
  });

  it("P4 leaves an unseen deleted membership and stale events unwritten", async () => {
    const w = await seedLaunchWorld(modules);
    const event = { ...membershipEvent("admin"), deleted: true };
    await unchanged(w, () =>
      w.t.mutation(internal.clerkWebhook.applyEvent, {
        eventId: "c1-unseen-delete",
        event,
      }),
    );
    expect(
      await w.t.run(async (ctx) =>
        (await ctx.db.query("memberships").collect()).find(
          (row) => row.clerkMembershipId === event.membershipId,
        ),
      ),
    ).toMatchObject({ active: false });
    await unchanged(w, () =>
      w.t.mutation(internal.clerkWebhook.applyEvent, {
        eventId: "c1-unseen-older",
        event: { ...event, deleted: false, updatedAt: NOW, occurredAt: NOW },
      }),
    );
  });

  it("R2-2 excludes a free drive sheet version and health sentinels from every row", async () => {
    const w = await seedLaunchWorld(modules);
    const version = "FC max 187; naissance 1990; note privee R2";
    await as(w.t, "admin").mutation(
      api.machineRegistry.attachDriveParameterSheet,
      {
        machineId: w.machine,
        storageId: await registryUpload(w),
        version,
        signedBy: "Synthetic signer",
        at: NOW,
      },
    );
    const written = await rows(w);
    expect(written.map((row) => row.action)).toContain(
      "registry.drive_sheet_attached",
    );
    for (const sentinel of [
      version,
      "187",
      "1990",
      "note privee",
      "Synthetic signer",
    ])
      expect(JSON.stringify(written)).not.toContain(sentinel);
    expect(written.slice(-1)[0]?.details?.version).toBeUndefined();
  });

  it("excludes an unvalidated internal medical version from audit details", async () => {
    const w = await seedLaunchWorld(modules);
    const version = "FC max 187; private medical version";
    const storageId = await w.t.run((ctx) =>
      ctx.storage.store(new Blob(["Synthetic document"])),
    );
    await w.t.mutation(internal.medicalParametersStore.insertVersion, {
      version,
      storageId,
      issuedAt: "2026-01-01",
      signedBy: "Synthetic signer",
      signature: "synthetic",
      zoneCeilingFraction: 0.8,
      minRiderAge: 18,
      hrMaxFormula: "tanaka",
    });
    expect((await rows(w)).slice(-1)[0]?.action).toBe(
      "medical_parameters.registered",
    );
    expect(JSON.stringify(await rows(w))).not.toContain(version);
  });

  it("R2-3 P3 leaves empty and identical physiology unwritten and records only changed fields", async () => {
    const w = await seedLaunchWorld(modules);
    const call = (args: { hrMax?: number | null; birthYear?: number | null }) =>
      as(w.t, "manager").mutation(api.training.setUserPhysiology, {
        userId: w.patient,
        ...args,
      });
    const before = await w.t.run((ctx) => ctx.db.get(w.patient));
    await unchanged(w, () => call({}));
    expect(await w.t.run((ctx) => ctx.db.get(w.patient))).toEqual(before);
    await unchanged(w, () => call({ hrMax: 180, birthYear: 1990 }));
    await observe(w, async () => {
      await call({ hrMax: 187, birthYear: 1990 });
      return [
        site(w, "physiology.updated", w.patient, "user", "manager", w.orgA, {
          fields: ["hrMax"],
        }),
      ];
    });
    await call({ hrMax: null });
    await unchanged(w, () => call({ hrMax: null }));
  });

  it("leaves an unchanged user role unwritten and records a real change", async () => {
    const w = await seedLaunchWorld(modules);
    configureAnheartOrganization(null);
    await w.t.run((ctx) =>
      ctx.db.patch(w.anheartOrg, { clerkOrgId: undefined }),
    );
    const call = (role: "admin" | "gestionnaire") =>
      w.t
        .withIdentity({ subject: "admin" })
        .mutation(api.users.updateUserRole, { userId: w.admin, role });
    await unchanged(w, () => call("admin"));
    await observe(w, async () => {
      await call("gestionnaire");
      return [
        site(w, "user.role_changed", w.admin, "user", "admin", w.anheartOrg, {
          role: "gestionnaire",
          previousRole: "admin",
        }),
      ];
    });
  });

  it("leaves empty and identical registry identity unwritten and lists only changed fields", async () => {
    const w = await seedLaunchWorld(modules);
    const call = (args: {
      site?: string | null;
      contacts?: Id<"users">[];
      serialPi?: string | null;
    }) =>
      as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
        machineId: w.machine,
        ...args,
      });
    await unchanged(w, () => call({}));
    await call({
      site: "Synthetic site",
      contacts: [w.manager],
      serialPi: null,
    });
    await unchanged(w, () =>
      call({
        site: "  Synthetic site  ",
        contacts: [w.manager],
        serialPi: null,
      }),
    );
    await observe(w, async () => {
      await call({ site: "Synthetic other site", contacts: [w.manager] });
      return [
        site(
          w,
          "registry.identity_updated",
          w.machine,
          "machine",
          "admin",
          w.orgA,
          { fields: ["site"] },
        ),
      ];
    });
  });

  it.each(["manager", "admin"] as const)(
    "uses affected organisations for physiology with a stale home as %s",
    async (actor) => {
      const w = await seedLaunchWorld(modules);
      await w.t.run((ctx) =>
        ctx.db.patch(w.patient, { organizationId: w.orgB }),
      );
      await observe(w, async () => {
        await as(w.t, actor).mutation(api.training.setUserPhysiology, {
          userId: w.patient,
          hrMax: 187,
        });
        return [
          site(w, "physiology.updated", w.patient, "user", actor, w.orgA, {
            fields: ["hrMax"],
          }),
        ];
      });
    },
  );

  it.each(["orgAdmin", "admin"] as const)(
    "uses removed memberships for deletion with a stale home as %s",
    async (actor) => {
      const w = await seedLaunchWorld(modules);
      await w.t.run((ctx) =>
        ctx.db.patch(w.patient, { organizationId: w.orgB }),
      );
      await observe(w, async () => {
        await as(w.t, actor).mutation(api.users.deleteUser, {
          userId: w.patient,
        });
        return [site(w, "user.deleted", w.patient, "user", actor, w.orgA)];
      });
    },
  );

  it("records the previous release level only on a level correction", async () => {
    const w = await seedLaunchWorld(modules);
    const releaseId = await registryRelease(w, "pi-0.1.0", "bench");
    await observe(w, async () => {
      await as(w.t, "admin").mutation(api.softwareReleases.recordRelease, {
        component: "pi",
        version: "pi-0.1.0",
        validationLevel: "auto_validated",
        releasedAt: NOW,
        notes: "Synthetic private correction",
      });
      return [
        site(
          w,
          "software_release.recorded",
          releaseId,
          "software_release",
          "admin",
          w.anheartOrg,
          {
            version: "pi-0.1.0",
            level: "auto_validated",
            previousLevel: "bench",
          },
        ),
      ];
    });
  });

  it("records the new owner when only machine ownership changes", async () => {
    const w = await seedLaunchWorld(modules);
    await as(w.t, "orgAdmin").mutation(
      api.machines.assignMachineToGestionnaires,
      { machineId: w.machine, gestionnaireIds: [w.manager, w.otherManager] },
    );
    await observe(w, async () => {
      await as(w.t, "orgAdmin").mutation(
        api.machines.assignMachineToGestionnaires,
        { machineId: w.machine, gestionnaireIds: [w.otherManager, w.manager] },
      );
      return [
        site(
          w,
          "machine_gestionnaires.set",
          w.machine,
          "machine",
          "orgAdmin",
          w.orgA,
          { added: [], removed: [], gestionnaireId: w.otherManager },
        ),
      ];
    });
  });
});

const sourceText = import.meta.glob(
  [
    "./**/*.ts",
    "!./_generated/**",
    "!./**/*.test.ts",
    "!./**/*.fixtures.ts",
    "!./**/*.setup.ts",
    "!./**/*.matrix.ts",
  ],
  { eager: true, query: "?raw", import: "default" },
) as Record<string, string>;
const exportedModules = import.meta.glob(
  [
    "./**/*.ts",
    "!./_generated/**",
    "!./convex.config.ts",
    "!./**/*.test.ts",
    "!./**/*.fixtures.ts",
    "!./**/*.setup.ts",
    "!./**/*.matrix.ts",
  ],
  { eager: true },
) as Record<string, Record<string, unknown>>;

function walk(node: ts.Node, visit: (node: ts.Node) => void) {
  visit(node);
  ts.forEachChild(node, (child) => walk(child, visit));
}
function registrationOf(node: ts.Node): ts.VariableDeclaration | undefined {
  let ancestor: ts.Node | undefined = node;
  while (ancestor) {
    if (
      ts.isVariableDeclaration(ancestor) &&
      ts.isIdentifier(ancestor.name) &&
      ancestor.initializer &&
      ts.isCallExpression(ancestor.initializer) &&
      ts.isIdentifier(ancestor.initializer.expression) &&
      [
        "mutation",
        "internalMutation",
        "action",
        "internalAction",
        "query",
        "internalQuery",
      ].includes(ancestor.initializer.expression.text)
    )
      return ancestor;
    ancestor = ancestor.parent;
  }
}

describe("ANH-167 T02-f operator isolation", () => {
  it("permits operator literals only inside the three registered internal functions and blocks public references", () => {
    const operatorFunctions = new Set<string>();
    const internalReferences: string[] = [];
    const forbidden = new Set([
      "createKeyByOperator",
      "revokeKeyByOperator",
      "insertVersion",
      "registerVersion",
    ]);
    for (const [path, source] of Object.entries(sourceText)) {
      const parsed = ts.createSourceFile(
        path,
        source,
        ts.ScriptTarget.Latest,
        true,
      );
      walk(parsed, (node) => {
        if (
          ts.isPropertyAssignment(node) &&
          node.name.getText(parsed) === "source" &&
          ts.isStringLiteral(node.initializer) &&
          node.initializer.text === "operator"
        ) {
          const registration = registrationOf(node);
          expect(registration, path).toBeDefined();
          if (!registration)
            throw new Error("Operator source outside a registered function");
          const name = registration.name.getText(parsed);
          operatorFunctions.add(`${path}:${name}`);
          const registered = exportedModules[path]?.[name] as
            | { isInternal?: boolean }
            | undefined;
          expect(registered?.isInternal, `${path}:${name}`).toBe(true);
        }
        if (
          ts.isPropertyAccessExpression(node) &&
          forbidden.has(node.name.text)
        ) {
          const registration = registrationOf(node);
          const owner = `${path}:${registration?.name.getText(parsed)}`;
          internalReferences.push(`${owner}:${node.name.text}`);
        }
        if (
          ts.isVariableDeclaration(node) &&
          node.initializer &&
          ts.isCallExpression(node.initializer) &&
          ts.isIdentifier(node.initializer.expression) &&
          ["mutation", "action", "query"].includes(
            node.initializer.expression.text,
          )
        ) {
          walk(node.initializer, (child) => {
            if (ts.isIdentifier(child))
              expect(
                forbidden.has(child.text),
                `${path}:${node.name.getText(parsed)} references ${child.text}`,
              ).toBe(false);
          });
        }
      });
    }
    expect([...operatorFunctions].sort()).toEqual([
      "./machineKeys.ts:createKeyByOperator",
      "./machineKeys.ts:revokeKeyByOperator",
      "./medicalParametersStore.ts:insertVersion",
    ]);
    expect(internalReferences).toEqual([
      "./medicalParameters.ts:registerVersion:insertVersion",
    ]);
    for (const name of forbidden)
      expect(sourceText["./http.ts"]).not.toContain(name);
    const registerVersion = exportedModules["./medicalParameters.ts"]
      .registerVersion as { isInternal?: boolean };
    expect(registerVersion.isInternal).toBe(true);
  });

  it.each(http.getRoutes().map(([path, method]) => ({ path, method })))(
    "never writes operator from $method $path",
    async ({ path, method }) => {
      vi.stubEnv(
        "CLERK_WEBHOOK_SIGNING_SECRET",
        `whsec_${btoa("synthetic-audit-webhook-key")}`,
      );
      const w = await seedMachineWorld(modules);
      const before = new Set((await rows(w)).map((row) => row._id));
      const response = await w.t.fetch(path, {
        method,
        headers: {
          Authorization: `Bearer ${w.machineKey}`,
          "Content-Type": "application/json",
          [CONTRACT_HEADER]: CONTRACT_VERSION,
        },
        ...(method === "POST" ? { body: "{}" } : {}),
      });
      expect(response.status).toBeLessThan(500);
      const written = (await rows(w)).filter((row) => !before.has(row._id));
      expect(written.every((row) => row.source !== "operator")).toBe(true);
    },
  );
});
