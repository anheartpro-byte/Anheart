import { expect, test } from "../fixtures";

test.describe("un admin Anheart connecté", () => {
  test.use({ account: "admin" });

  test("voit le tableau de bord en français puis en anglais, avec son nom", async ({ page, run }) => {
    const { firstName, lastName } = run.accounts.admin;
    await page.goto("/fr/dashboard");
    await expect(page.getByRole("heading", { name: "Tableau de bord" })).toBeVisible();
    await expect(page.getByText(`Bienvenue, ${firstName}`)).toBeVisible();
    await expect(page.getByText(`${firstName} ${lastName}`)).toBeVisible();
    await expect(page.getByRole("link", { name: "Audit" })).toBeVisible();

    await page.getByRole("button", { name: "Francais" }).click();
    await page.getByRole("menuitem", { name: "English" }).click();

    await expect(page).toHaveURL(/\/en\/dashboard$/);
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByText(`Welcome, ${firstName}`)).toBeVisible();
    await expect(page.getByText(`${firstName} ${lastName}`)).toBeVisible();
  });
});

test.describe("un passager connecté", () => {
  test.use({ account: "passagerA" });

  test("n'a pas les pages d'administration", async ({ page, run }) => {
    await page.goto("/fr/dashboard");
    await expect(page.getByText(`Bienvenue, ${run.accounts.passagerA.firstName}`)).toBeVisible();
    await expect(page.getByRole("link", { name: "Mes machines" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Audit" })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Utilisateurs" })).toHaveCount(0);

    await page.goto("/fr/dashboard/audit");
    await expect(page.getByText("L'accès au journal est réservé aux administrateurs.")).toBeVisible();

    await page.goto("/en/dashboard/settings");
    await expect(page.getByText("Access denied. Admin only.")).toBeVisible();
  });
});
