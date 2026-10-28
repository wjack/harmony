import { useMutation, useQueryClient } from "@tanstack/react-query";

import { describeError, unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import type { SessionView } from "../api/types";
import { useApi } from "../app/context";
import { toast } from "../shell/toasts";

/**
 * Demo controls' Skip approvals (ARCHITECTURE.md §11.4): the world's switch, carried on
 * the session's world view. The toggle moves at once and goes back if the server refuses.
 */
export function useSetSkipApprovals() {
  const api = useApi();
  const queryClient = useQueryClient();
  const setSkip = (skip: boolean) => {
    queryClient.setQueryData<SessionView>(
      queryKeys.session,
      (session) => session && { ...session, world: { ...session.world, skip_approvals: skip } },
    );
  };
  return useMutation({
    mutationFn: (skip: boolean) => unwrap(api.PUT("/api/demo/approvals", { body: { skip } })),
    onMutate: async (skip) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.session });
      const previous = queryClient.getQueryData<SessionView>(queryKeys.session)?.world.skip_approvals;
      setSkip(skip);
      return { previous };
    },
    onSuccess: ({ skip }) => {
      setSkip(skip);
    },
    onError: (error, _skip, context) => {
      if (context?.previous !== undefined) setSkip(context.previous);
      toast({ text: `Couldn't change approvals: ${describeError(error)}`, key: "skip-approvals" });
    },
  });
}
