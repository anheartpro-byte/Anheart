"use client";

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import { useRouter } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
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
} from "@/components/ui/form";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Loader2, AlertCircle } from "lucide-react";

const AVAILABLE_CHANNELS = ["ECG", "EMG", "EDA", "EEG", "ACC", "LUX"];

const sessionSchema = z.object({
  machineId: z.string().min(1, "Please select a machine"),
  userId: z.string().min(1, "Please select a patient"),
  channels: z.array(z.string()).min(1, "Select at least one channel"),
  notes: z.string().optional(),
});

type SessionFormValues = z.infer<typeof sessionSchema>;

interface SessionFormModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess?: (sessionId: Id<"sessions">) => void;
}

export function SessionFormModal({
  open,
  onOpenChange,
  onSuccess,
}: SessionFormModalProps) {
  const t = useTranslations();
  const router = useRouter();

  // Fetch online machines and patients
  const machines = useQuery(api.machines.listMachines, { status: "online" });
  const patients = useQuery(api.users.listUsers, { role: "user" });

  const createSession = useMutation(api.sessions.createSession);

  const form = useForm<SessionFormValues>({
    resolver: zodResolver(sessionSchema),
    defaultValues: {
      machineId: "",
      userId: "",
      channels: ["ECG"],
      notes: "",
    },
  });

  // Reset form when modal opens
  useEffect(() => {
    if (open) {
      form.reset({
        machineId: "",
        userId: "",
        channels: ["ECG"],
        notes: "",
      });
    }
  }, [open, form]);

  const onSubmit = async (values: SessionFormValues) => {
    try {
      const sessionId = await createSession({
        machineId: values.machineId as Id<"machines">,
        userId: values.userId as Id<"users">,
        channels: values.channels,
        notes: values.notes || undefined,
      });

      onOpenChange(false);

      if (onSuccess) {
        onSuccess(sessionId);
      } else {
        // Redirect to live view by default
        router.push(`/dashboard/sessions/${sessionId}/live`);
      }
    } catch (error) {
      form.setError("root", {
        message: error instanceof Error ? error.message : "An error occurred",
      });
    }
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

  const handleClose = () => {
    form.reset();
    onOpenChange(false);
  };

  const isLoading = machines === undefined || patients === undefined;
  const noOnlineMachines = machines?.length === 0;
  const noPatients = patients?.length === 0;

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("sessions.create")}</DialogTitle>
          <DialogDescription>
            {t("sessions.createDescription") ||
              "Start a new ECG recording session"}
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        ) : noOnlineMachines ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <AlertCircle className="h-12 w-12 text-amber-500 mb-4" />
            <p className="text-muted-foreground">
              {t("sessions.noOnlineMachines") ||
                "No machines are currently online. Please wait for a machine to connect."}
            </p>
          </div>
        ) : noPatients ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <AlertCircle className="h-12 w-12 text-amber-500 mb-4" />
            <p className="text-muted-foreground">
              {t("sessions.noPatients") ||
                "No patients found. Please create a patient first."}
            </p>
          </div>
        ) : (
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
              {form.formState.errors.root && (
                <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm">
                  {form.formState.errors.root.message}
                </div>
              )}

              <FormField
                control={form.control}
                name="machineId"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t("sessions.selectMachine")} *</FormLabel>
                    <Select
                      onValueChange={field.onChange}
                      defaultValue={field.value}
                    >
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue
                            placeholder={
                              t("sessions.selectMachinePlaceholder") ||
                              "Select a machine"
                            }
                          />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {machines?.map((machine) => (
                          <SelectItem key={machine._id} value={machine._id}>
                            {machine.name}
                            {machine.location && ` (${machine.location})`}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="userId"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t("sessions.selectPatient")} *</FormLabel>
                    <Select
                      onValueChange={field.onChange}
                      defaultValue={field.value}
                    >
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue
                            placeholder={
                              t("sessions.selectPatientPlaceholder") ||
                              "Select a patient"
                            }
                          />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {patients?.map((patient) => (
                          <SelectItem key={patient._id} value={patient._id}>
                            {patient.firstName} {patient.lastName} (
                            {patient.email})
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="channels"
                render={() => (
                  <FormItem>
                    <FormLabel>{t("sessions.selectChannels")} *</FormLabel>
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

              <FormField
                control={form.control}
                name="notes"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t("sessions.notes")}</FormLabel>
                    <FormControl>
                      <Textarea
                        placeholder={
                          t("sessions.notesPlaceholder") || "Optional notes..."
                        }
                        rows={3}
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <div className="flex gap-3 pt-4">
                <Button
                  type="submit"
                  disabled={form.formState.isSubmitting}
                  className="flex-1"
                >
                  {form.formState.isSubmitting && (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  )}
                  {t("sessions.start")}
                </Button>
                <Button type="button" variant="outline" onClick={handleClose}>
                  {t("common.cancel")}
                </Button>
              </div>
            </form>
          </Form>
        )}
      </DialogContent>
    </Dialog>
  );
}
