"use client";

import { useState, useEffect, useMemo, useRef } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useMutationWithFeedback } from "@/hooks/use-mutation-with-feedback";
import { pushFeedback } from "@/lib/feedback";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import {
  offersGestionnaireChoice,
  submitGestionnaireList,
} from "@/lib/machineForm";
import { isAnheartAdmin } from "@/lib/organizationAccess";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
  FormDescription,
} from "@/components/ui/form";
import { Copy, Check, AlertTriangle, Loader2 } from "lucide-react";

// Validation messages are passed in so they follow the active locale.
const createMachineSchema = (messages: { nameRequired: string }) =>
  z.object({
    name: z.string().min(1, messages.nameRequired).max(100),
    location: z.string().max(200).optional(),
    gestionnaireIds: z.array(z.string()).optional(),
    // Creation by the Anheart admin only: the machine's organisation.
    organizationId: z.string().optional(),
  });

type MachineFormValues = z.infer<ReturnType<typeof createMachineSchema>>;

interface MachineFormModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  machine?: {
    _id: Id<"machines">;
    organizationId?: Id<"organizations">;
    name: string;
    location?: string;
    gestionnaires?: Array<{
      _id: Id<"users">;
      firstName: string;
      lastName: string;
      isOwner: boolean;
    }>;
  };
  onSuccess?: () => void;
}

export function MachineFormModal({
  open,
  onOpenChange,
  machine,
  onSuccess,
}: MachineFormModalProps) {
  const t = useTranslations();
  const isEditing = !!machine;

  const [apiKey, setApiKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const currentUser = useQuery(api.users.getCurrentUser);
  // Only the Anheart admin creates machines, in any organisation: they choose
  // it. Everyone else works in the organisation of the machine, or their own.
  const choosesOrganization =
    !isEditing && isAnheartAdmin(currentUser?.role);
  const organizations = useQuery(
    api.organizations.listOrganizations,
    choosesOrganization ? {} : "skip",
  );
  const [chosenOrganization, setChosenOrganization] = useState<
    Id<"organizations"> | undefined
  >(undefined);
  const machineOrganization = isEditing
    ? machine.organizationId
    : choosesOrganization
      ? (chosenOrganization ?? currentUser?.organization?._id)
      : undefined;
  // Only the gestionnaires of the machine's organisation can be assigned: the
  // server leaves out any other, so the form does not offer them, and offers
  // none for a machine that has no organisation yet (lib/machineForm.ts).
  const choosesGestionnaires = offersGestionnaireChoice(machine);
  const gestionnaires = useQuery(
    api.users.listGestionnaires,
    !choosesGestionnaires
      ? "skip"
      : machineOrganization
        ? { organizationId: machineOrganization }
        : {},
  );
  const createMachine = useMutationWithFeedback(api.machines.createMachine);
  const updateMachine = useMutationWithFeedback(api.machines.updateMachine);
  const assignMachineToGestionnaires = useMutationWithFeedback(
    api.machines.assignMachineToGestionnaires,
  );

  const machineSchema = useMemo(
    () =>
      createMachineSchema({
        nameRequired: t("machines.form.nameRequired"),
      }),
    [t],
  );

  const form = useForm<MachineFormValues>({
    resolver: zodResolver(machineSchema),
    defaultValues: {
      name: "",
      location: "",
      gestionnaireIds: [],
    },
  });

  // The form is filled when the window opens, or when it is pointed at another
  // machine, and only then. The machine object is a new one at every
  // heartbeat of an online machine (its `lastHeartbeat`, the server's clock):
  // refilling the form then would erase what is being typed and the error on
  // screen. `filledFor` is the machine the form was filled for ("" for a new
  // machine), null while the window is closed.
  const filledFor = useRef<string | null>(null);
  useEffect(() => {
    const target = open ? (machine?._id ?? "") : null;
    if (target !== null && target !== filledFor.current) {
      setChosenOrganization(undefined);
      form.reset({
        name: machine?.name ?? "",
        location: machine?.location ?? "",
        gestionnaireIds: machine?.gestionnaires?.map((g) => g._id) ?? [],
      });
    }
    filledFor.current = target;
  }, [open, machine, form]);

  const onSubmit = async (values: MachineFormValues) => {
    const showFailure = (message: string) => form.setError("root", { message });
    if (isEditing) {
      // An admin's edit is two calls. The success is said once both have
      // passed, below: never for the name and the place alone.
      const updated = await updateMachine({
        machineId: machine._id,
        name: values.name,
        location: values.location || undefined,
      });
      if (!updated.ok) return showFailure(updated.message);
      // The gestionnaire list is an admin-only call. Whether it is made is
      // decided in lib/machineForm.ts, never here: only for a machine whose
      // gestionnaires the form offers, for a caller allowed to choose them,
      // and only when the list changed. A gestionnaire saving the name or
      // the place never makes it.
      const assigned = choosesGestionnaires
        ? await submitGestionnaireList({
            role: currentUser?.role,
            current: machine.gestionnaires?.map((g) => g._id) ?? [],
            selected: values.gestionnaireIds as Id<"users">[] | undefined,
            assign: (gestionnaireIds) =>
              assignMachineToGestionnaires({
                machineId: machine._id,
                gestionnaireIds,
              }),
          })
        : null;
      // `assigned` is null when the call was not made: nothing was refused.
      // When it is refused, the name and the place are already saved: the
      // window says so, with the reason the list was not.
      if (assigned && !assigned.ok) {
        return showFailure(
          t("machines.form.partiallySaved", { reason: assigned.message }),
        );
      }
      pushFeedback("success", t("machines.updateSuccess"));
      onOpenChange(false);
      onSuccess?.();
    } else {
      const created = await createMachine(
        {
          name: values.name,
          location: values.location || undefined,
          gestionnaireIds: values.gestionnaireIds as Id<"users">[] | undefined,
          organizationId: machineOrganization,
        },
        { success: t("machines.createSuccess") },
      );
      if (!created.ok) return showFailure(created.message);
      setApiKey(created.value.apiKey);
    }
  };

  const copyApiKey = async () => {
    if (apiKey) {
      await navigator.clipboard.writeText(apiKey);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const handleClose = () => {
    const hadApiKey = !!apiKey;
    setApiKey(null);
    form.reset();
    onOpenChange(false);
    if (hadApiKey) onSuccess?.();
  };

  // Show API key dialog after creation
  if (apiKey) {
    return (
      <Dialog open={open} onOpenChange={handleClose}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("machines.apiKey")}</DialogTitle>
            <DialogDescription className="flex items-center gap-2 text-amber-600">
              <AlertTriangle className="h-4 w-4" />
              {t("machines.apiKeyWarning")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="bg-muted p-4 rounded-md font-mono text-sm break-all">
              {apiKey}
            </div>
            <div className="flex gap-2">
              <Button onClick={copyApiKey} variant="outline" className="flex-1">
                {copied ? (
                  <>
                    <Check className="h-4 w-4 mr-2" />
                    {t("common.copied")}
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4 mr-2" />
                    {t("common.copy")}
                  </>
                )}
              </Button>
              <Button onClick={handleClose} className="flex-1">
                {t("common.close")}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {isEditing ? t("common.edit") : t("machines.create")}
          </DialogTitle>
          <DialogDescription>
            {isEditing
              ? t("machines.form.editDescription")
              : t("machines.form.createDescription")}
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
            {form.formState.errors.root && (
              <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm">
                {form.formState.errors.root.message}
              </div>
            )}

            <FormField
              control={form.control}
              name="name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t("machines.name")} *</FormLabel>
                  <FormControl>
                    <Input
                      placeholder={t("machines.form.namePlaceholder")}
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="location"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t("machines.location")}</FormLabel>
                  <FormControl>
                    <Input
                      placeholder={t("machines.form.locationPlaceholder")}
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            {/* Organisation of a new machine (Anheart admin) */}
            {choosesOrganization && organizations && organizations.length > 0 && (
              <FormItem>
                <FormLabel>{t("machines.form.organization")}</FormLabel>
                <Select
                  value={machineOrganization}
                  onValueChange={(value) => {
                    setChosenOrganization(value as Id<"organizations">);
                    // The boxes were those of the previous organisation.
                    form.setValue("gestionnaireIds", []);
                  }}
                >
                  <FormControl>
                    <SelectTrigger className="w-full">
                      <SelectValue
                        placeholder={t("machines.form.organizationPlaceholder")}
                      />
                    </SelectTrigger>
                  </FormControl>
                  <SelectContent>
                    {organizations.map((organization) => (
                      <SelectItem key={organization._id} value={organization._id}>
                        {organization.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <FormDescription>
                  {t("machines.form.organizationDescription")}
                </FormDescription>
              </FormItem>
            )}

            {/* A machine from before the multi-organisation migration */}
            {!choosesGestionnaires && (
              <p className="text-sm text-muted-foreground">
                {t("machines.form.noOrganization")}
              </p>
            )}

            {/* Gestionnaire Assignment (for admin creating/editing) */}
            {gestionnaires && gestionnaires.length > 0 && (
              <FormField
                control={form.control}
                name="gestionnaireIds"
                render={() => (
                  <FormItem>
                    <FormLabel>{t("machines.assignGestionnaires")}</FormLabel>
                    <FormDescription>
                      {t("machines.assignGestionnairesDesc")}
                    </FormDescription>
                    <div className="space-y-2 max-h-40 overflow-y-auto border rounded-md p-3">
                      {gestionnaires.map((g) => {
                        const isSelected = form
                          .watch("gestionnaireIds")
                          ?.includes(g._id);
                        return (
                          <div
                            key={g._id}
                            className="flex items-center space-x-2"
                          >
                            <Checkbox
                              id={g._id}
                              checked={isSelected}
                              onCheckedChange={(checked) => {
                                const current =
                                  form.getValues("gestionnaireIds") || [];
                                if (checked) {
                                  form.setValue("gestionnaireIds", [
                                    ...current,
                                    g._id,
                                  ]);
                                } else {
                                  form.setValue(
                                    "gestionnaireIds",
                                    current.filter((id) => id !== g._id),
                                  );
                                }
                              }}
                            />
                            <label
                              htmlFor={g._id}
                              className="text-sm font-medium leading-none cursor-pointer"
                            >
                              {g.firstName} {g.lastName}
                              <span className="text-muted-foreground ml-2">
                                ({g.email})
                              </span>
                            </label>
                          </div>
                        );
                      })}
                    </div>
                    <FormMessage />
                  </FormItem>
                )}
              />
            )}

            <div className="flex gap-3 pt-4">
              <Button
                type="submit"
                disabled={form.formState.isSubmitting}
                className="flex-1"
              >
                {form.formState.isSubmitting && (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                )}
                {isEditing ? t("common.save") : t("common.create")}
              </Button>
              <Button type="button" variant="outline" onClick={handleClose}>
                {t("common.cancel")}
              </Button>
            </div>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
