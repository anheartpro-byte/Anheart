import {
  click,
  render,
  submit,
  type,
  type Screen,
} from "@/test-support/render";
import { zodResolver } from "@hookform/resolvers/zod";
import type { ReactNode } from "react";
import { useForm } from "react-hook-form";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { draw } from "@/test-support/markup";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
  useFormField,
} from "./form";
import { Input } from "./input";

/**
 * The parts of a form field, on the real react-hook-form and the real zod, as
 * the machine and the patient forms use them. What the parts do together: the
 * label, the field, its description and its error message are tied by ids a
 * screen reader follows; a refused value marks the field invalid, paints the
 * label and shows the message of the error; a corrected value clears it all.
 */

const NAME_REQUIRED = "Le nom est requis";
const schema = z.object({ name: z.string().min(1, NAME_REQUIRED) });
type Values = z.infer<typeof schema>;

/** What the server does when it refuses a name and gives no reason. */
const REFUSE_WITHOUT_A_REASON = "Refuse without a reason";

function MachineForm({
  onValid = () => {},
  hint,
  className,
  children,
}: {
  onValid?: (values: Values) => void;
  /** A text given to the message part, shown while there is no error. */
  hint?: string;
  /** A class given to every part that takes one. */
  className?: string;
  /** More parts, drawn inside the item of the field. */
  children?: ReactNode;
}) {
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { name: "" },
  });
  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit((values) => onValid(values))}>
        <FormField
          control={form.control}
          name="name"
          render={({ field }) => (
            <FormItem className={className}>
              <FormLabel className={className}>Nom</FormLabel>
              <FormControl>
                {/* The type is said: the document of the tests has no default one, and React asks. */}
                <Input type="text" {...field} />
              </FormControl>
              <FormDescription className={className}>
                Visible par les gestionnaires
              </FormDescription>
              <FormMessage className={className}>{hint}</FormMessage>
              {children}
            </FormItem>
          )}
        />
        <button
          type="button"
          onClick={() => form.setError("name", { type: "server" })}
        >
          {REFUSE_WITHOUT_A_REASON}
        </button>
      </form>
    </Form>
  );
}

/** The elements a part of the form marked `data-slot="name"`. */
function parts(screen: Screen, name: string) {
  return screen.all((element) => element.getAttribute("data-slot") === name);
}

/** The one element marked `data-slot="name"`. */
function part(screen: Screen, name: string) {
  const found = parts(screen, name);
  if (found.length !== 1) {
    throw new Error(`${found.length} parts "${name}": ${screen.markup()}`);
  }
  return found[0];
}

function classes(screen: Screen, name: string) {
  return part(screen, name).className.split(" ");
}

describe("ANH-203 form field: before anything is refused", () => {
  it("ties the label and the description to the field by ids", () => {
    const screen = render(<MachineForm />);

    const field = part(screen, "form-control");
    const description = part(screen, "form-description");
    expect(field.id).toMatch(/.-form-item$/);
    expect(part(screen, "form-label").getAttribute("for")).toBe(field.id);
    expect(description.id).toBe(`${field.id}-description`);
    expect(field.getAttribute("aria-describedby")).toBe(description.id);
    expect(screen.textOf(description)).toBe("Visible par les gestionnaires");
  });

  it("marks the caller's own field as the control, with no element around it", () => {
    const screen = render(<MachineForm />);

    const field = part(screen, "form-control");
    expect(field.localName).toBe("input");
    expect(field.getAttribute("name")).toBe("name");
    expect(field.parentElement).toBe(part(screen, "form-item"));
  });

  it("says the field is valid, and shows no message", () => {
    const screen = render(<MachineForm />);

    expect(part(screen, "form-control").getAttribute("aria-invalid")).toBe(
      "false",
    );
    expect(part(screen, "form-label").getAttribute("data-error")).toBe("false");
    expect(parts(screen, "form-message")).toEqual([]);
  });

  it("shows the hint the caller gives to the message part", () => {
    const screen = render(<MachineForm hint="100 caractères au plus" />);

    const message = part(screen, "form-message");
    expect(message.localName).toBe("p");
    expect(screen.textOf(message)).toBe("100 caractères au plus");
    expect(message.id).toBe(`${part(screen, "form-control").id}-message`);
  });

  it("gives each field of a form ids of its own", () => {
    const screen = render(
      <>
        <MachineForm />
        <MachineForm />
      </>,
    );

    const [first, second] = parts(screen, "form-control");
    expect(first.id).not.toBe(second.id);
    const [firstLabel, secondLabel] = parts(screen, "form-label");
    expect(firstLabel.getAttribute("for")).toBe(first.id);
    expect(secondLabel.getAttribute("for")).toBe(second.id);
  });
});

describe("ANH-203 form field: a refused value", () => {
  it("shows the message of the error under the field", async () => {
    const onValid = vi.fn();
    const screen = render(<MachineForm onValid={onValid} />);

    await submit(screen.form());

    const message = part(screen, "form-message");
    expect(screen.textOf(message)).toBe(NAME_REQUIRED);
    expect(message.id).toBe(`${part(screen, "form-control").id}-message`);
    expect(classes(screen, "form-message")).toContain("text-destructive");
    expect(onValid).not.toHaveBeenCalled();
  });

  it("marks the field invalid and ties it to the message as well", async () => {
    const screen = render(<MachineForm />);

    await submit(screen.form());

    const field = part(screen, "form-control");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(field.getAttribute("aria-describedby")).toBe(
      `${part(screen, "form-description").id} ${part(screen, "form-message").id}`,
    );
  });

  it("paints the label as in error", async () => {
    const screen = render(<MachineForm />);

    await submit(screen.form());

    expect(part(screen, "form-label").getAttribute("data-error")).toBe("true");
    // The class that paints it reads that mark.
    expect(classes(screen, "form-label")).toContain(
      "data-[error=true]:text-destructive",
    );
  });

  it("shows the error in place of the hint", async () => {
    const screen = render(<MachineForm hint="100 caractères au plus" />);

    await submit(screen.form());

    expect(screen.textOf(part(screen, "form-message"))).toBe(NAME_REQUIRED);
    expect(screen.text()).not.toContain("100 caractères au plus");
  });

  it("clears the error once the value is corrected, and lets the form go", async () => {
    const onValid = vi.fn();
    const screen = render(<MachineForm onValid={onValid} />);
    await submit(screen.form());

    await type(part(screen, "form-control"), "Centri Paris");
    await submit(screen.form());

    expect(onValid).toHaveBeenCalledWith({ name: "Centri Paris" });
    expect(parts(screen, "form-message")).toEqual([]);
    expect(part(screen, "form-control").getAttribute("aria-invalid")).toBe(
      "false",
    );
    expect(part(screen, "form-label").getAttribute("data-error")).toBe("false");
  });

  it("marks the field invalid but draws no message when the error has none", async () => {
    const screen = render(<MachineForm hint="100 caractères au plus" />);

    await click(screen.button(REFUSE_WITHOUT_A_REASON));

    expect(part(screen, "form-control").getAttribute("aria-invalid")).toBe(
      "true",
    );
    expect(part(screen, "form-label").getAttribute("data-error")).toBe("true");
    // Neither an empty paragraph nor the hint: the hint is for a field with no error.
    expect(parts(screen, "form-message")).toEqual([]);
  });
});

describe("ANH-203 form field: the caller's classes and props", () => {
  it("lets the caller's class replace the one of each part", async () => {
    const screen = render(<MachineForm className="gap-4 text-xs" />);
    await submit(screen.form());

    // The item is a grid that spaces its parts.
    expect(classes(screen, "form-item")).toContain("grid");
    expect(classes(screen, "form-item")).toContain("gap-4");
    expect(classes(screen, "form-item")).not.toContain("gap-2");
    for (const name of ["form-label", "form-description", "form-message"]) {
      expect(classes(screen, name)).toContain("text-xs");
      expect(classes(screen, name)).not.toContain("text-sm");
    }
  });

  it("passes the other props of the item, the description and the message on", () => {
    const page = draw(
      <MachineForm hint="100 caractères au plus">
        <FormItem title="item" />
        <FormDescription title="description" />
        <FormMessage title="message">Autre</FormMessage>
      </MachineForm>,
    );

    for (const title of ["item", "description", "message"]) {
      expect(
        page.all((element) => element.getAttribute("title") === title),
      ).toHaveLength(1);
    }
  });
});

describe("ANH-203 useFormField", () => {
  /** A part of the caller's own: it shows what the hook gives it. */
  function Probe() {
    const {
      name,
      id,
      formItemId,
      formDescriptionId,
      formMessageId,
      invalid,
      error,
    } = useFormField();
    return (
      <output>
        {JSON.stringify({
          name,
          id,
          formItemId,
          formDescriptionId,
          formMessageId,
          invalid,
          error: error?.message ?? null,
        })}
      </output>
    );
  }

  function given(screen: Screen) {
    return JSON.parse(screen.textOf(screen.tag("output")[0]));
  }

  it("gives a part the name of its field and the ids of its item", () => {
    const screen = render(
      <MachineForm>
        <Probe />
      </MachineForm>,
    );

    const seen = given(screen);
    expect(seen.name).toBe("name");
    expect(seen.formItemId).toBe(`${seen.id}-form-item`);
    expect(seen.formDescriptionId).toBe(`${seen.id}-form-item-description`);
    expect(seen.formMessageId).toBe(`${seen.id}-form-item-message`);
    // The same ids as the parts of the item carry.
    expect(part(screen, "form-control").id).toBe(seen.formItemId);
    expect(part(screen, "form-description").id).toBe(seen.formDescriptionId);
  });

  it("gives a part the state of its field, before and after a refused value", async () => {
    const screen = render(
      <MachineForm>
        <Probe />
      </MachineForm>,
    );
    expect(given(screen)).toMatchObject({ invalid: false, error: null });

    await submit(screen.form());

    expect(given(screen)).toMatchObject({
      invalid: true,
      error: NAME_REQUIRED,
    });
  });

  it("cannot be used outside a form: the part is refused at once", () => {
    expect(() =>
      draw(
        <FormItem>
          <FormLabel>Nom</FormLabel>
        </FormItem>,
      ),
    ).toThrow(TypeError);
  });
});
