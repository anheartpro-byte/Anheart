import { useCallback } from "react";
import { useAction } from "convex/react";
import {
  getFunctionName,
  type FunctionArgs,
  type FunctionReference,
  type FunctionReturnType,
} from "convex/server";
import { useTranslations } from "next-intl";
import {
  runWithFeedback,
  type MutationFeedback,
  type MutationOutcome,
} from "./use-mutation-with-feedback";

/**
 * The only way the site calls a Convex action: the same contract as
 * `useMutationWithFeedback` (never rejects, every outcome shown).
 */
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
    ): Promise<MutationOutcome<FunctionReturnType<Action>>> =>
      runWithFeedback(`[action] ${name}`, () => run(args), feedback, t),
    [run, name, t],
  );
}
