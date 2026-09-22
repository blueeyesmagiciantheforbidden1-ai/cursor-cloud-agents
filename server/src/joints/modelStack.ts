const IDS = ["codex", "claude", "cursor", "copilot", "grok"] as const;

export function selectStack(): { ids: string[]; apiBillingAllowed: boolean } {
  return {
    ids: [...IDS],
    apiBillingAllowed: false,
  };
}
