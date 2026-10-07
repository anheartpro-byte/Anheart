import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import ConvexClientProvider from "./ConvexClientProvider";

/**
 * Where the site connects to Convex, and with whose identity: the address
 * comes from the configuration of the deployment, never from the code, and
 * the session is the one Clerk holds.
 */

const made = vi.hoisted(() => {
  // Read when the module under test is loaded: it must be set before the imports run.
  process.env.NEXT_PUBLIC_CONVEX_URL = "https://unit-test.convex.cloud";
  return {
    addresses: [] as string[],
    providers: [] as { client: unknown; useAuth: unknown }[],
  };
});
const useAuth = vi.hoisted(() => () => ({ isSignedIn: true }));

vi.mock("convex/react", () => ({
  ConvexReactClient: class {
    readonly address: string;
    constructor(address: string) {
      this.address = address;
      made.addresses.push(address);
    }
  },
}));
vi.mock("convex/react-clerk", () => ({
  ConvexProviderWithClerk: ({
    children,
    client,
    useAuth: auth,
  }: {
    children: ReactNode;
    client: unknown;
    useAuth: unknown;
  }) => {
    made.providers.push({ client, useAuth: auth });
    return <div data-convex="">{children}</div>;
  },
}));
vi.mock("@clerk/nextjs", () => ({ useAuth }));

describe("ANH-203 connection of the site to Convex", () => {
  it("connects once, to the address of the deployment's configuration", () => {
    expect(made.addresses).toEqual(["https://unit-test.convex.cloud"]);
  });

  it("gives Convex the session Clerk holds, and draws the page inside the connection", () => {
    const html = renderToStaticMarkup(
      <ConvexClientProvider>
        <p>Contenu de la page</p>
      </ConvexClientProvider>,
    );

    expect(html).toBe('<div data-convex=""><p>Contenu de la page</p></div>');
    expect(made.providers).toHaveLength(1);
    expect(made.providers[0].useAuth).toBe(useAuth);
    expect(made.providers[0].client).toMatchObject({
      address: "https://unit-test.convex.cloud",
    });
    // Drawing the page again opens no second connection.
    renderToStaticMarkup(<ConvexClientProvider>{null}</ConvexClientProvider>);
    expect(made.addresses).toHaveLength(1);
  });
});
