import { useCallback } from "react";
import { useAction } from "convex/react";
import {
  getFunctionName,
  type FunctionArgs,
  type FunctionReference,
  type FunctionReturnType,
} from "convex/server";
import { useTranslations } from "next-intl";
import { describeMutationError, pushFeedback } from "@/lib/feedback";
import type {
  MutationFeedback,
  MutationOutcome,
} from "./use-mutation-with-feedback";

export function useActionWithFeedback<
  Action extends FunctionReference<"action">,
>(action: Action) {
  const run = useAction(action);
  const t = useTranslations();
  const name = getFunctionName(action);
  return useCallback(
    async (
      args: FunctionArgs<Action>,
      feedback?: MutationFeedback<FunctionReturnType<Action>>,
    ): Promise<MutationOutcome<FunctionReturnType<Action>>> => {
      let value: FunctionReturnType<Action>;
      try {
        value = await run(args);
      } catch (error) {
        const failure = describeMutationError(error, t);
        console.error(
          `[action] ${name} failed`,
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
    [run, name, t],
  );
}
