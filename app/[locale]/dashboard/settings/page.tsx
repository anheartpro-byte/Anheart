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
        role: selectedRole as "admin" | "gestionnaire" | "technician" | "user",
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
        <p className="text-muted-foreground">Access denied. Admin only.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("nav.settings")}</h1>
        <p className="text-muted-foreground">Manage application settings</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6">
        {/* Admin Info */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <User className="h-4 w-4" />
              Your Account
            </CardTitle>
            <CardDescription>Your admin account information</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label className="text-muted-foreground">Name</Label>
                <p className="font-medium">
                  {user?.firstName} {user?.lastName}
                </p>
              </div>
              <div>
                <Label className="text-muted-foreground">Email</Label>
                <p className="font-medium">{user?.email}</p>
              </div>
              <div>
                <Label className="text-muted-foreground">Role</Label>
                <Badge variant="default">
                  {t(`users.roles.${user?.role}`)}
                </Badge>
              </div>
              <div>
                <Label className="text-muted-foreground">Language</Label>
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
              User Role Management
            </CardTitle>
            <CardDescription>
              Change user roles across the system
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label>Select User</Label>
              <Select value={selectedUserId} onValueChange={setSelectedUserId}>
                <SelectTrigger>
                  <SelectValue placeholder="Choose a user..." />
                </SelectTrigger>
                <SelectContent>
                  {users
                    ?.filter((u) => u._id !== user?._id)
                    .map((u) => (
                      <SelectItem key={u._id} value={u._id}>
                        {u.firstName} {u.lastName} ({u.email}) - {u.role}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>New Role</Label>
              <Select value={selectedRole} onValueChange={setSelectedRole}>
                <SelectTrigger>
                  <SelectValue placeholder="Choose a role..." />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="admin">
                    {t("users.roles.admin")}
                  </SelectItem>
                  <SelectItem value="gestionnaire">
                    {t("users.roles.gestionnaire")}
                  </SelectItem>
                  <SelectItem value="technician">
                    {t("users.roles.technician")}
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
              {updating ? t("common.loading") : "Update Role"}
            </Button>
          </CardContent>
        </Card>

        {/* System Info */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Globe className="h-4 w-4" />
              System Information
            </CardTitle>
            <CardDescription>Application configuration</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <Label className="text-muted-foreground">Application</Label>
                <p className="font-medium">AnHeart ECG Monitoring</p>
              </div>
              <div>
                <Label className="text-muted-foreground">Version</Label>
                <p className="font-medium">1.0.0</p>
              </div>
              <div>
                <Label className="text-muted-foreground">
                  Default Language
                </Label>
                <p className="font-medium">French (fr)</p>
              </div>
              <div>
                <Label className="text-muted-foreground">
                  Supported Languages
                </Label>
                <p className="font-medium">French, English</p>
              </div>
            </div>
            <Separator />
            <div className="text-sm text-muted-foreground">
              <p>Heartbeat timeout: 90 seconds</p>
              <p>Default sample rate: 1000 Hz</p>
              <p>Default batch interval: 1000 ms</p>
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
