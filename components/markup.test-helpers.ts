/**
 * Reading the static markup of a component in a test, without a browser: the
 * markup is the one React produced a line earlier, read one character at a
 * time.
 */

/** The text of a markup: each tag is replaced by `between`. */
export function textOf(html: string, between = " "): string {
  let text = "";
  let inTag = false;
  for (const character of html) {
    if (character === "<") {
      inTag = true;
      text += between;
    } else if (character === ">") {
      inTag = false;
    } else if (!inTag) {
      text += character;
    }
  }
  return text;
}

/** Whether the button that holds `label` is drawn disabled. */
export function buttonDisabled(html: string, label: string): boolean {
  const labelAt = html.indexOf(label);
  const tagAt = html.lastIndexOf("<button", labelAt);
  if (labelAt === -1 || tagAt === -1) {
    throw new Error(`No button holds "${label}"`);
  }
  const tag = html.slice(tagAt, html.indexOf(">", tagAt));
  return tag.includes(' disabled=""');
}
