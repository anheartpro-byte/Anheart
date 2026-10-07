/**
 * Stand-ins for the windows, the lists of choices and the check boxes of the
 * site (`components/ui/dialog`, `select`, `checkbox`), for the test of a
 * component that uses them.
 *
 * The real ones are built on Radix UI, which needs a browser (layout, focus,
 * portals) that the document of `test-support/dom` is not. These keep what a
 * test of the component above them needs: a window draws its content only
 * while it is open and can be closed, a choice can be made, a box can be
 * ticked. The real wrappers have their own tests in `components/ui/`.
 *
 *     vi.mock("@/components/ui/dialog", async () =>
 *       (await import("@/test-support/ui")).dialog,
 *     );
 *     vi.mock("@/components/ui/select", async () =>
 *       (await import("@/test-support/ui")).select,
 *     );
 *     vi.mock("@/components/ui/checkbox", async () =>
 *       (await import("@/test-support/ui")).checkbox,
 *     );
 */

import { createContext, useContext, useState, type ReactNode } from "react";
import { click, type Screen } from "./render";

type Children = { children?: ReactNode; className?: string };

/** How a test names the control that closes a window: Escape, the cross or a click outside, in a browser. */
const CLOSE_WINDOW = "close-window";

function Dialog({
  open,
  onOpenChange,
  children,
}: Children & { open?: boolean; onOpenChange?: (open: boolean) => void }) {
  if (!open) return null;
  return (
    <div role="dialog">
      {children}
      <button
        type="button"
        aria-label={CLOSE_WINDOW}
        onClick={() => onOpenChange?.(false)}
      />
    </div>
  );
}

const Block = ({ children, className }: Children) => (
  <div className={className}>{children}</div>
);

/** What stands for `@/components/ui/dialog`. */
export const dialog = {
  Dialog,
  DialogContent: Block,
  DialogHeader: Block,
  DialogFooter: Block,
  DialogTitle: ({ children }: Children) => <h2>{children}</h2>,
  DialogDescription: Block,
};

type Choice = {
  value: string | undefined;
  choose: (value: string) => void;
  disabled: boolean;
};
const ChoiceContext = createContext<Choice>({
  value: undefined,
  choose: () => {},
  disabled: false,
});

function Select({
  value,
  defaultValue,
  onValueChange,
  disabled = false,
  children,
}: Children & {
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  disabled?: boolean;
}) {
  // Like the real one: it keeps the choice itself when the caller gives no `value`.
  const [own, setOwn] = useState(defaultValue);
  const current = value !== undefined ? value : own;
  const choose = (next: string) => {
    setOwn(next);
    onValueChange?.(next);
  };
  return (
    <ChoiceContext.Provider value={{ value: current, choose, disabled }}>
      <div data-select="" data-value={current ?? ""}>
        {children}
      </div>
    </ChoiceContext.Provider>
  );
}

/** Shows the placeholder while nothing is chosen. The chosen option says so itself (`aria-selected`). */
function SelectValue({ placeholder }: { placeholder?: string }) {
  const { value } = useContext(ChoiceContext);
  return value ? null : <span data-placeholder="">{placeholder}</span>;
}

function SelectItem({ value, children }: Children & { value: string }) {
  const choice = useContext(ChoiceContext);
  return (
    <button
      type="button"
      role="option"
      aria-selected={choice.value === value}
      disabled={choice.disabled}
      onClick={() => choice.choose(value)}
    >
      {children}
    </button>
  );
}

/** What stands for `@/components/ui/select`. */
export const select = {
  Select,
  SelectTrigger: Block,
  SelectContent: Block,
  SelectValue,
  SelectItem,
};

function Checkbox({
  id,
  checked,
  onCheckedChange,
}: {
  id?: string;
  checked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      id={id}
      aria-checked={checked === true}
      onClick={() => onCheckedChange?.(checked !== true)}
    />
  );
}

/** What stands for `@/components/ui/checkbox`. */
export const checkbox = { Checkbox };

// --- Acting on them ---------------------------------------------------------------

/** Whether a window is open on the page. */
export function windowOpen(screen: Screen): boolean {
  return (
    screen.all((element) => element.getAttribute("role") === "dialog").length >
    0
  );
}

/** Closes the open window as Escape, its cross or a click outside would. */
export async function closeWindow(screen: Screen) {
  const [control] = screen.all(
    (element) => element.getAttribute("aria-label") === CLOSE_WINDOW,
  );
  if (control === undefined) throw new Error("No window is open");
  await click(control);
}

/** The options of the lists of the page, as they read, in order. */
export function options(screen: Screen): string[] {
  return screen
    .all((element) => element.getAttribute("role") === "option")
    .map((element) => screen.textOf(element));
}

/** Chooses the option that reads `label` in a list of the page. */
export async function choose(screen: Screen, label: string) {
  const found = screen.all(
    (element) =>
      element.getAttribute("role") === "option" &&
      screen.textOf(element) === label,
  );
  if (found.length !== 1) {
    throw new Error(
      `${found.length} options read "${label}". The options: ${options(screen).join(" | ")}`,
    );
  }
  await click(found[0]);
}

/** The option that is chosen in each list of the page, as it reads. */
export function chosen(screen: Screen): string[] {
  return screen
    .all((element) => element.getAttribute("aria-selected") === "true")
    .map((element) => screen.textOf(element));
}
