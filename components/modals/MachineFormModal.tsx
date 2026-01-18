"use client";

import { useState, useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
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
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
  FormDescription,
} from "@/components/ui/form";
import { Copy, Check, AlertTriangle, Loader2 } from "lucide-react";

// Available BITalino sensor channels (A1-A6)
const AVAILABLE_CHANNELS = ["ECG", "EDA", "SpO2", "RESP", "EMG", "LUX"];

const machineSchema = z.object({
  name: z.string().min(1, "Name is required").max(100),
  location: z.string().max(200).optional(),
  sampleRate: z.number().min(100).max(10000),
  batchInterval: z.number().min(100).max(5000),
  channels: z.array(z.string()).min(1, "Select at least one channel"),
  gestionnaireIds: z.array(z.string()).optional(),
});

type MachineFormValues = z.infer<typeof machineSchema>;

interface MachineFormModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  machine?: {
    _id: Id<"machines">;
    name: string;
    location?: string;
    config: {
      sampleRate: number;
      channels: string[];
      batchInterval: number;
    };
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

  const gestionnaires = useQuery(api.users.listGestionnaires);
  const createMachine = useMutation(api.machines.createMachine);
  const updateMachine = useMutation(api.machines.updateMachine);
  const assignMachineToGestionnaires = useMutation(
    api.machines.assignMachineToGestionnaires,
  );

  const form = useForm<MachineFormValues>({
    resolver: zodResolver(machineSchema),
    defaultValues: {
      name: "",
      location: "",
      sampleRate: 100,
      batchInterval: 1000,
      channels: ["ECG"],
      gestionnaireIds: [],
    },
  });

  // Reset form when machine changes or modal opens
  useEffect(() => {
    if (open) {
      form.reset({
        name: machine?.name ?? "",
        location: machine?.location ?? "",
        sampleRate: machine?.config.sampleRate ?? 100,
        batchInterval: machine?.config.batchInterval ?? 1000,
        channels: machine?.config.channels ?? ["ECG"],
        gestionnaireIds: machine?.gestionnaires?.map((g) => g._id) ?? [],
      });
    }
  }, [open, machine, form]);

  const onSubmit = async (values: MachineFormValues) => {
    try {
      if (isEditing) {
        await updateMachine({
          machineId: machine._id,
          name: values.name,
          location: values.location || undefined,
          config: {
            sampleRate: values.sampleRate,
            channels: values.channels,
            batchInterval: values.batchInterval,
          },
        });
        // Update gestionnaire assignments
        if (values.gestionnaireIds) {
          await assignMachineToGestionnaires({
            machineId: machine._id,
            gestionnaireIds: values.gestionnaireIds as Id<"users">[],
          });
        }
        onOpenChange(false);
        onSuccess?.();
      } else {
        const result = await createMachine({
          name: values.name,
          location: values.location || undefined,
          config: {
            sampleRate: values.sampleRate,
            channels: values.channels,
            batchInterval: values.batchInterval,
          },
          gestionnaireIds: values.gestionnaireIds as Id<"users">[] | undefined,
        });
        setApiKey(result.apiKey);
      }
    } catch (error) {
      form.setError("root", {
        message: error instanceof Error ? error.message : "An error occurred",
      });
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

  const toggleChannel = (channel: string) => {
    const current = form.getValues("channels");
    if (current.includes(channel)) {
      form.setValue(
        "channels",
        current.filter((c) => c !== channel),
        { shouldValidate: true },
      );
    } else {
      form.setValue("channels", [...current, channel], {
        shouldValidate: true,
      });
    }
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
              ? "Update machine configuration"
              : "Configure a new Raspberry Pi machine"}
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
                    <Input placeholder="Raspberry Pi 1" {...field} />
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
                    <Input placeholder="Room 101" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className="grid grid-cols-2 gap-4">
              <FormField
                control={form.control}
                name="sampleRate"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t("machines.sampleRate")} (Hz)</FormLabel>
                    <FormControl>
                      <Input
                        type="number"
                        min={100}
                        max={10000}
                        {...field}
                        onChange={(e) =>
                          field.onChange(parseInt(e.target.value) || 1000)
                        }
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="batchInterval"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t("machines.batchInterval")}</FormLabel>
                    <FormControl>
                      <Input
                        type="number"
                        min={100}
                        max={5000}
                        {...field}
                        onChange={(e) =>
                          field.onChange(parseInt(e.target.value) || 1000)
                        }
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <FormField
              control={form.control}
              name="channels"
              render={() => (
                <FormItem>
                  <FormLabel>{t("machines.channels")} *</FormLabel>
                  <div className="flex flex-wrap gap-2">
                    {AVAILABLE_CHANNELS.map((channel) => (
                      <Button
                        key={channel}
                        type="button"
                        variant={
                          form.watch("channels").includes(channel)
                            ? "default"
                            : "outline"
                        }
                        size="sm"
                        onClick={() => toggleChannel(channel)}
                      >
                        {channel}
                      </Button>
                    ))}
                  </div>
                  <FormMessage />
                </FormItem>
              )}
            />

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
