"use client";

import { useState, use } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { useRouter, Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Pencil,
  Trash2,
  User,
  Mail,
  Globe,
  Calendar,
  Activity,
  ArrowLeft,
  Shield,
  Building,
} from "lucide-react";
import { format, formatDistanceToNow } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { PatientFormModal } from "@/components/modals/PatientFormModal";

export default function UserDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();

  const userId = id as Id<"users">;
  const user = useQuery(api.users.getUserById, { userId });
  const currentUser = useQuery(api.users.getCurrentUser);
  const sessions = useQuery(api.sessions.listSessions, { userId, limit: 10 });

  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  const deleteUser = useMutation(api.users.deleteUser);

  const dateLocale = locale === "fr" ? fr : enUS;

  const canManage =
    currentUser?.role === "admin" || currentUser?.role === "gestionnaire";
  const canDelete =
    currentUser?.role === "admin" ||
    (currentUser?.role === "gestionnaire" && user?.role === "user");

  const handleDelete = async () => {
    try {
      await deleteUser({ userId });
      router.push("/dashboard/users");
    } catch (err) {
      console.error(err);
    }
  };

  if (user === undefined || currentUser === undefined) {
    return <UserDetailSkeleton />;
  }

  if (user === null) {
    return (
      <div className="text-center py-12">
        <User className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">{t("users.noUsers")}</p>
        <Link
          href="/dashboard/users"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  const getRoleIcon = (role: string) => {
    switch (role) {
      case "admin":
        return <Shield className="h-4 w-4" />;
      case "gestionnaire":
        return <Building className="h-4 w-4" />;
      default:
        return <User className="h-4 w-4" />;
    }
  };

  const getRoleBadgeVariant = (role: string) => {
    switch (role) {
      case "admin":
        return "default";
      case "gestionnaire":
        return "secondary";
      default:
        return "outline";
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link href="/dashboard/users">
            <Button variant="ghost" size="icon">
              <ArrowLeft className="h-4 w-4" />
            </Button>
          </Link>
          <div>
            <h1 className="text-2xl font-bold">
              {user.firstName} {user.lastName}
            </h1>
            <p className="text-muted-foreground">{user.email}</p>
          </div>
        </div>
        {canManage && user.role === "user" && (
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setShowEditModal(true)}>
              <Pencil className="h-4 w-4 mr-2" />
              {t("common.edit")}
            </Button>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* User Information Card */}
        <Card className="lg:col-span-1">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <User className="h-4 w-4" />
              {t("reports.patientInfo")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-3">
              <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center">
                {getRoleIcon(user.role)}
              </div>
              <div>
                <p className="font-medium text-lg">
                  {user.firstName} {user.lastName}
                </p>
                <Badge
                  variant={
                    getRoleBadgeVariant(user.role) as
                      | "default"
                      | "secondary"
                      | "outline"
                  }
                >
                  {t(`users.roles.${user.role}`)}
                </Badge>
              </div>
            </div>

            <Separator />

            <div className="space-y-3">
              <div className="flex items-center gap-2 text-sm">
                <Mail className="h-4 w-4 text-muted-foreground" />
                <span>{user.email}</span>
              </div>
              <div className="flex items-center gap-2 text-sm">
                <Globe className="h-4 w-4 text-muted-foreground" />
                <span>{user.language === "fr" ? "Francais" : "English"}</span>
              </div>
              <div className="flex items-center gap-2 text-sm">
                <Shield className="h-4 w-4 text-muted-foreground" />
                <span>
                  {t("users.role")}: {t(`users.roles.${user.role}`)}
                </span>
              </div>
              <div className="flex items-center gap-2 text-sm">
                <Calendar className="h-4 w-4 text-muted-foreground" />
                <span>
                  {t("users.createdAt")}:{" "}
                  {format(user.createdAt, "PPP", { locale: dateLocale })}
                </span>
              </div>
            </div>

            {canDelete && user._id !== currentUser?._id && (
              <>
                <Separator />
                <Button
                  variant="destructive"
                  className="w-full"
                  onClick={() => setShowDeleteDialog(true)}
                >
                  <Trash2 className="h-4 w-4 mr-2" />
                  {t("common.delete")}
                </Button>
              </>
            )}
          </CardContent>
        </Card>

        {/* User Details / Sessions Card */}
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Activity className="h-4 w-4" />
              {user.role === "user"
                ? t("dashboard.recentSessions")
                : t("users.title")}
            </CardTitle>
            <CardDescription>
              {user.role === "user"
                ? `${sessions?.length ?? 0} ${t("nav.sessions").toLowerCase()}`
                : "User account details"}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {user.role === "user" ? (
              // Show sessions for patients
              sessions === undefined ? (
                <div className="space-y-2">
                  <Skeleton className="h-12 w-full" />
                  <Skeleton className="h-12 w-full" />
                  <Skeleton className="h-12 w-full" />
                </div>
              ) : sessions.length === 0 ? (
                <div className="text-center py-8">
                  <Activity className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                  <p className="text-muted-foreground">
                    {t("sessions.noSessions")}
                  </p>
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("sessions.machine")}</TableHead>
                      <TableHead>{t("machines.status")}</TableHead>
                      <TableHead>{t("sessions.startedAt")}</TableHead>
                      <TableHead>{t("sessions.duration")}</TableHead>
                      <TableHead>{t("common.actions")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sessions.map((session) => (
                      <TableRow key={session._id}>
                        <TableCell className="font-medium">
                          {session.machineName}
                        </TableCell>
                        <TableCell>
                          <SessionStatusBadge status={session.status} />
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {format(session.startedAt, "PPp", {
                            locale: dateLocale,
                          })}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {session.endedAt
                            ? formatDuration(
                                session.endedAt - session.startedAt,
                              )
                            : formatDistanceToNow(session.startedAt, {
                                locale: dateLocale,
                              })}
                        </TableCell>
                        <TableCell>
                          {session.status === "active" ? (
                            <Link
                              href={`/dashboard/sessions/${session._id}/live`}
                            >
                              <Button variant="default" size="sm">
                                {t("sessions.viewLive")}
                              </Button>
                            </Link>
                          ) : session.status === "completed" ? (
                            <Link href={`/dashboard/sessions/${session._id}`}>
                              <Button variant="outline" size="sm">
                                {t("sessions.viewReport")}
                              </Button>
                            </Link>
                          ) : (
                            <span className="text-muted-foreground">-</span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )
            ) : (
              // Show additional info for non-patient users
              <div className="space-y-6">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">Full Name</p>
                    <p className="font-medium">
                      {user.firstName} {user.lastName}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">
                      Email Address
                    </p>
                    <p className="font-medium">{user.email}</p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">Role</p>
                    <Badge
                      variant={
                        getRoleBadgeVariant(user.role) as
                          | "default"
                          | "secondary"
                          | "outline"
                      }
                    >
                      {t(`users.roles.${user.role}`)}
                    </Badge>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">Language</p>
                    <p className="font-medium">
                      {user.language === "fr" ? "Francais" : "English"}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">
                      Account Created
                    </p>
                    <p className="font-medium">
                      {format(user.createdAt, "PPPp", { locale: dateLocale })}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">User ID</p>
                    <p className="font-mono text-xs text-muted-foreground">
                      {user._id}
                    </p>
                  </div>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Delete Confirmation Dialog */}
      <Dialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("users.deleteConfirm")}</DialogTitle>
            <DialogDescription>
              This action cannot be undone. This will permanently delete the
              user &quot;{user.firstName} {user.lastName}&quot; and all
              associated data.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowDeleteDialog(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleDelete}>
              {t("common.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit User Modal (only for patients) */}
      {user.role === "user" && (
        <PatientFormModal
          open={showEditModal}
          onOpenChange={setShowEditModal}
          patient={user}
        />
      )}
    </div>
  );
}

function SessionStatusBadge({ status }: { status: string }) {
  const t = useTranslations("sessions");

  const variants: Record<
    string,
    "default" | "secondary" | "destructive" | "outline"
  > = {
    active: "default",
    completed: "secondary",
    pending: "outline",
    failed: "destructive",
  };

  return (
    <Badge variant={variants[status] || "outline"}>
      {t(status as "active" | "completed" | "pending" | "failed")}
    </Badge>
  );
}

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) {
    return `${hours}h ${minutes % 60}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
}

function UserDetailSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Skeleton className="h-10 w-10" />
        <div>
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-32 mt-2" />
        </div>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <Skeleton className="h-80" />
        <Skeleton className="h-80 lg:col-span-2" />
      </div>
    </div>
  );
}
