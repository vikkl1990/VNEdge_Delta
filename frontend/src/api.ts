// Thin fetch client over the existing token-gated dashboard endpoints.
// The token is read from the ?token= query param (same convention as the
// classic dashboard) and sent as a Bearer header — never placed in logs.

export function getToken(): string {
  return new URLSearchParams(window.location.search).get("token") ?? "";
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (res.status === 401) throw new ApiError(401, "unauthorized — check the token");
  if (!res.ok) throw new ApiError(res.status, `request failed (${res.status})`);
  return (await res.json()) as T;
}

// --- Response shapes (only the fields the panels read) ---------------------
export interface WhoAmI {
  name: string | null;
  role: string | null;
  permissions: string[];
}

export interface Position {
  symbol?: string;
  side?: string;
  quantity?: number;
  entry_price?: number;
  unrealized_pnl_usd?: number;
  [k: string]: unknown;
}

export interface PriceBook {
  bid?: number;
  ask?: number;
  mid?: number;
  spread_bps?: number;
}

export interface FeedHealth {
  exchange?: string;
  candles?: string;
  funding?: string;
  open_interest?: string;
  last_update_ms?: number;
}

export interface Snapshot {
  mode?: string;
  symbol?: string;
  price?: PriceBook | null;
  funding_rate?: number | null;
  feed_health?: FeedHealth;
  equity?: number;
  peak_equity?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  daily_pnl?: number;
  consecutive_losses?: number;
  kill_switch_active?: boolean;
  live_trading_enabled?: boolean;
  risk_status?: string;
  positions?: Position[];
  fills?: number;
  fees_usd?: number;
  [k: string]: unknown;
}

export interface JournalRow {
  lane?: string;
  symbol?: string;
  side?: string;
  net_pnl_usd?: number;
  exit_reason?: string;
  [k: string]: unknown;
}
