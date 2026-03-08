"use client";

import { useState } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";

import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Save, Shield, Globe, User } from "lucide-react";

export default function SettingsPage() {
  const t = useTranslations();
  const user = useQuery(api.users.getCurrentUser);
  const users = useQuery(api.users.listUsers, {});
  const updateUserRole = useMutation(api.users.updateUserRole);

  const [selectedUserId, setSelectedUserId] = useState<string>("");
  const [selectedRole, setSelectedRole] = useState<string>("");
  const [updating, setUpdating] = useState(false);

  const handleRoleUpdate = async () => {
    if (!selectedUserId || !selectedRole) return;

    setUpdating(true);
    try {
      await updateUserRole({
        userId: selectedUserId as Parameters<
          typeof updateUserRole
        >[0]["userId"],
        role: selectedRole as "admin" | "gestionnaire" | "user",
      });
      setSelectedUserId("");
      setSelectedRole("");
    } catch (err) {
      console.error(err);
    } finally {
      setUpdating(false);
    }
  };

  if (user === undefined) {
    return <SettingsSkeleton />;
  }

  // Only admin can access settings
  if (user?.role !== "admin") {
    return (
      <div className="text-center py-12">
        <Shield className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
        <p className="text-muted-foreground">{t("settings.accessDenied")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("settings.title")}</h1>
        <p className="text-muted-foreground">{t("settings.description")}</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
        {/* Admin Info */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <User className="h-4 w-4" />
              {t("settings.yourAccount")}
            </CardTitle>
            <CardDescription>{t("settings.yourAccountDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="min-w-0">
                <Label className="text-muted-foreground">
                  {t("settings.name")}
                </Label>
                <p className="font-medium truncate">
                  {user?.firstName} {user?.lastName}
                </p>
              </div>
              <div className="min-w-0">
                <Label className="text-muted-foreground">
                  {t("users.email")}
                </Label>
                <p className="font-medium truncate" title={user?.email}>
                  {user?.email}
                </p>
              </div>
              <div>
                <Label className="text-muted-foreground">
                  {t("users.role")}
                </Label>
                <Badge variant="default">
                  {t(`users.roles.${user?.role}`)}
                </Badge>
              </div>
              <div>
                <Label className="text-muted-foreground">
                  {t("settings.language")}
                </Label>
                <p className="font-medium uppercase">{user?.language}</p>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Role Management */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Shield className="h-4 w-4" />
              {t("settings.roleManagement")}
            </CardTitle>
            <CardDescription>
              {t("settings.roleManagementDesc")}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label>{t("settings.selectUser")}</Label>
              <Select value={selectedUserId} onValueChange={setSelectedUserId}>
                <SelectTrigger className="w-full">
                  <SelectValue
                    placeholder={t("settings.selectUserPlaceholder")}
                  />
                </SelectTrigger>
                <SelectContent className="max-w-[var(--radix-select-trigger-width)]">
                  {users
                    ?.filter((u) => u._id !== user?._id)
                    .map((u) => (
                      <SelectItem
                        key={u._id}
                        value={u._id}
                        className="truncate"
                      >
                        <span className="truncate">
                          {u.firstName} {u.lastName}
                        </span>
                        <span className="text-muted-foreground ml-1">
                          - {t(`users.roles.${u.role}`)}
                        </span>
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>{t("settings.newRole")}</Label>
              <Select value={selectedRole} onValueChange={setSelectedRole}>
                <SelectTrigger>
                  <SelectValue
                    placeholder={t("settings.selectRolePlaceholder")}
                  />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="admin">
                    {t("users.roles.admin")}
                  </SelectItem>
                  <SelectItem value="gestionnaire">
                    {t("users.roles.gestionnaire")}
                  </SelectItem>
                  <SelectItem value="user">{t("users.roles.user")}</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <Button
              onClick={handleRoleUpdate}
              disabled={!selectedUserId || !selectedRole || updating}
            >
              <Save className="h-4 w-4 mr-2" />
              {updating ? t("common.loading") : t("settings.updateRole")}
            </Button>
          </CardContent>
        </Card>

        {/* System Info */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Globe className="h-4 w-4" />
              {t("settings.systemInfo")}
            </CardTitle>
            <CardDescription>{t("settings.systemInfoDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <Label className="text-muted-foreground">
                  {t("settings.application")}
                </Label>
                <p className="font-medium">Gaura ECG Monitoring</p>
              </div>
              <div>
                <Label className="text-muted-foreground">
                  {t("settings.version")}
                </Label>
                <p className="font-medium">1.0.0</p>
              </div>
              <div>
                <Label className="text-muted-foreground">
                  {t("settings.defaultLanguage")}
                </Label>
                <p className="font-medium">{t("settings.french")}</p>
              </div>
              <div>
                <Label className="text-muted-foreground">
                  {t("settings.supportedLanguages")}
                </Label>
                <p className="font-medium">{t("settings.languages")}</p>
              </div>
            </div>
            <Separator />
            <div className="text-sm text-muted-foreground">
              <p>{t("settings.heartbeatTimeout")}: 90s</p>
              <p>{t("settings.defaultSampleRate")}: 1000 Hz</p>
              <p>{t("settings.defaultBatchInterval")}: 1000 ms</p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function SettingsSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-4 w-48 mt-2" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
        <Skeleton className="h-48" />
        <Skeleton className="h-64" />
        <Skeleton className="h-48" />
      </div>
    </div>
  );
}
