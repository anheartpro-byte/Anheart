"use client";

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
import { Loader2, CheckCircle } from "lucide-react";

const patientSchema = z.object({
  firstName: z.string().min(1, "First name is required").max(100),
  lastName: z.string().min(1, "Last name is required").max(100),
  email: z.string().email("Invalid email address"),
  language: z.enum(["fr", "en"]),
});

type PatientFormValues = z.infer<typeof patientSchema>;

interface PatientFormModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  patient?: {
    _id: Id<"users">;
    firstName: string;
    lastName: string;
    email: string;
    language: string;
  };
  onSuccess?: () => void;
}

export function PatientFormModal({
  open,
  onOpenChange,
  patient,
  onSuccess,
}: PatientFormModalProps) {
  const t = useTranslations();
  const isEditing = !!patient;

  const createPatient = useMutation(api.users.createPatient);
  const updatePatient = useMutation(api.users.updatePatient);

  const form = useForm<PatientFormValues>({
    resolver: zodResolver(patientSchema),
    defaultValues: {
      firstName: "",
      lastName: "",
      email: "",
      language: "fr",
    },
  });

  // Reset form when patient changes or modal opens
  useEffect(() => {
    if (open) {
      form.reset({
        firstName: patient?.firstName ?? "",
        lastName: patient?.lastName ?? "",
        email: patient?.email ?? "",
        language: (patient?.language as "fr" | "en") ?? "fr",
      });
    }
  }, [open, patient, form]);

  const onSubmit = async (values: PatientFormValues) => {
    try {
      if (isEditing && patient) {
        await updatePatient({
          userId: patient._id,
          firstName: values.firstName,
          lastName: values.lastName,
          language: values.language,
        });
        onOpenChange(false);
        onSuccess?.();
      } else {
        await createPatient({
          firstName: values.firstName,
          lastName: values.lastName,
          email: values.email,
          language: values.language,
        });
        onOpenChange(false);
        onSuccess?.();
      }
    } catch (error) {
      form.setError("root", {
        message: error instanceof Error ? error.message : "An error occurred",
      });
    }
  };

  const handleClose = () => {
    form.reset();
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {isEditing ? t("users.editPatient") : t("users.createPatient")}
          </DialogTitle>
          <DialogDescription>
            {isEditing
              ? t("users.editPatientDescription") ||
                "Update patient information"
              : t("users.createPatientDescription") ||
                "Add a new patient to your organization"}
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
            {form.formState.errors.root && (
              <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm">
                {form.formState.errors.root.message}
              </div>
            )}

            <div className="grid grid-cols-2 gap-4">
              <FormField
                control={form.control}
                name="firstName"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t("users.firstName")} *</FormLabel>
                    <FormControl>
                      <Input placeholder="John" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="lastName"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t("users.lastName")} *</FormLabel>
                    <FormControl>
                      <Input placeholder="Doe" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <FormField
              control={form.control}
              name="email"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t("users.email")} *</FormLabel>
                  <FormControl>
                    <Input
                      type="email"
                      placeholder="john.doe@example.com"
                      disabled={isEditing}
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="language"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t("users.language")} *</FormLabel>
                  <Select
                    onValueChange={field.onChange}
                    defaultValue={field.value}
                  >
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      <SelectItem value="fr">Francais</SelectItem>
                      <SelectItem value="en">English</SelectItem>
                    </SelectContent>
                  </Select>
                  <FormMessage />
                </FormItem>
              )}
            />

            {!isEditing && (
              <div className="bg-muted/50 p-3 rounded-md text-sm text-muted-foreground flex items-start gap-2">
                <CheckCircle className="h-4 w-4 mt-0.5 text-green-600" />
                <span>
                  {t("users.patientInviteNote") ||
                    "The patient will receive an email invitation to set up their account."}
                </span>
              </div>
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
