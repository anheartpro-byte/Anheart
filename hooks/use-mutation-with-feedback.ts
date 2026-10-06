import { useCallback } from "react";
import { useMutation } from "convex/react";
import {
  getFunctionName,
  type FunctionArgs,
  type FunctionReference,
  type FunctionReturnType,
  type OptionalRestArgs,
} from "convex/server";
import { useTranslations } from "next-intl";
import {
  describeMutationError,
  pushFeedback,
  type MutationFailure,
} from "@/lib/feedback";

export type MutationFeedback<Value> = {
  /**
   * Shown when the mutation succeeds: a text, or a text made from what the
   * mutation returned. Omit for a background mutation.
   */
  success?: string | ((value: Value) => string);
};

export type MutationOutcome<Value> =
  | { ok: true; value: Value }
  | ({ ok: false } & MutationFailure);

/**
 * The only way the site calls a Convex mutation: no failure stays silent.
 *
 * The returned function runs the mutation and never rejects. On success it
 * shows the caller's message; on failure it shows the translated error (see
 * `describeMutationError`) and logs it with the Convex request identifier.
 * The caller reads `ok` to decide what happens next.
 */
export function useMutationWithFeedback<
  Mutation extends FunctionReference<"mutation">,
>(mutation: Mutation) {
  const mutate = useMutation(mutation);
  const t = useTranslations();
  // `api.x.y` is a new object on every read: the name is what stays stable.
  const name = getFunctionName(mutation);

  return useCallback(
    async (
      ...params: [
        ...OptionalRestArgs<Mutation>,
        feedback?: MutationFeedback<FunctionReturnType<Mutation>>,
      ]
    ): Promise<MutationOutcome<FunctionReturnType<Mutation>>> => {
      // TypeScript cannot index a tuple whose head is still generic.
      const [args, feedback] = params as unknown as [
        FunctionArgs<Mutation> | undefined,
        MutationFeedback<FunctionReturnType<Mutation>> | undefined,
      ];
      let value: FunctionReturnType<Mutation>;
      try {
        value = await mutate(...([args] as OptionalRestArgs<Mutation>));
      } catch (error) {
        const failure = describeMutationError(error, t);
        console.error(
          `[mutation] ${name} failed`,
          { requestId: failure.requestId, code: failure.code },
          error,
        );
        pushFeedback("error", failure.message);
        return { ok: false, ...failure };
      }
      const success =
        typeof feedback?.success === "function"
          ? feedback.success(value)
          : feedback?.success;
      if (success) pushFeedback("success", success);
      return { ok: true, value };
    },
    [mutate, name, t],
  );
}
