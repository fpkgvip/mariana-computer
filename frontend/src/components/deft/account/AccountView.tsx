/**
 * AccountView — pure presentation for /account.
 *
 * Renders identity, balance + FIFO bucket detail, plan + subscription state,
 * the last 30 ledger transactions, and the side actions (Manage billing,
 * Add credits, Admin, Sign out).  All data is passed in via props so the
 * same component drives both the production /account route and the
 * /dev/account preview.
 *
 * Voice: calm, technical, no hype.  No emojis, no exclamation points.
 * Numbers should always look like receipts.
 */
import { Link } from "react-router-dom";
import {
  CreditCard,
  ExternalLink,
  Loader2,
  LogOut,
  Plus,
  ShieldCheck,
  Inbox,
  HelpCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types

export type SubscriptionStatus =
  | "active"
  | "canceled"
  | "past_due"
  | "trialing"
  | "none";

export interface CreditBucket {
  id: string;
  source: "grant" | "topup" | "refund" | "promo" | "trial";
  label: string; // e.g. "Pro plan grant" or "Top-up · 1,000"
  remaining: number;
  original: number;
  granted_at: string; // ISO
  expires_at?: string | null; // ISO
}

export type LedgerType = "grant" | "spend" | "refund" | "expiry" | "topup";

export interface LedgerTx {
  id: string;
  type: LedgerType;
  credits: number; // positive integer; sign derived from `type`
  ref_label: string; // human label, e.g. "Habit tracker run"
  ref_link?: string | null;
  created_at: string; // ISO
}

export interface Plan {
  id: string;
  name: string; // "Free" | "Starter" | "Standard" | "Pro" | "Scale"
  price_usd_monthly: number;
  credits_per_month: number;
}

export interface AccountData {
  name: string;
  email: string;
  role: "user" | "admin";
  plan: Plan;
  subscriptionStatus: SubscriptionStatus;
  nextRenewal?: string | null; // ISO
  balance: number; // total remaining credits across all buckets
  buckets: CreditBucket[]; // FIFO ordered, oldest first
  transactions: LedgerTx[]; // most recent 30, newest first
}

export interface AccountViewProps {
  data: AccountData;
  isOpeningPortal: boolean;
  onManageBilling: () => void;
  onLogout: () => void;
  onNavigateAdmin?: () => void;
  /** When true the Add credits link is shown next to Manage billing. */
  showAddCredits?: boolean;
}

// ---------------------------------------------------------------------------
// Formatters

const STATUS_CHIPS: Record<
  SubscriptionStatus,
  { label: string; className: string }
> = {
  active: { label: "Active", className: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30" },
  canceled: { label: "Canceled", className: "bg-rose-500/15 text-rose-300 ring-rose-500/30" },
  past_due: { label: "Past due", className: "bg-amber-500/15 text-amber-300 ring-amber-500/30" },
  trialing: { label: "Trialing", className: "bg-sky-500/15 text-sky-300 ring-sky-500/30" },
  none: { label: "None", className: "bg-zinc-500/15 text-zinc-300 ring-zinc-500/30" },
};

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Date.now() - then;
  const min = Math.round(diff / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const d = Math.round(hr / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(iso).toLocaleDateString();
}

function formatDate(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatCredits(n: number): string {
  return n.toLocaleString();
}

// 1c = $0.01
function creditsToUsd(c: number): string {
  return `$${(c / 100).toFixed(2)}`;
}

// ---------------------------------------------------------------------------
// Subcomponents

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[10.5px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
      {children}
    </p>
  );
}

function HeaderRow({ name, email, role }: { name: string; email: string; role: "user" | "admin" }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground sm:text-[28px]">
          Account
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {name} <span className="opacity-60">·</span> {email}
        </p>
      </div>
      {role === "admin" && (
        <span className="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2.5 py-0.5 text-[11px] font-medium text-accent ring-1 ring-accent/30">
          <ShieldCheck size={11} aria-hidden /> Admin
        </span>
      )}
    </div>
  );
}

function BalanceCard({ balance, buckets }: { balance: number; buckets: CreditBucket[] }) {
  return (
    <section
      aria-labelledby="acct-balance"
      className="rounded-xl border border-border/70 bg-surface-1/60 p-5"
    >
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <SectionLabel>Balance</SectionLabel>
          <p
            id="acct-balance"
            className="mt-1 font-mono text-3xl font-semibold tracking-tight text-foreground"
          >
            {formatCredits(balance)}
            <span className="ml-2 text-[12px] font-normal text-muted-foreground">
              credits
            </span>
          </p>
          <p className="mt-1 text-[11.5px] text-muted-foreground">
            ≈ {creditsToUsd(balance)} at 1c = $0.01
          </p>
        </div>
        <Link
          to="/checkout"
          className="inline-flex items-center gap-1 rounded-md border border-border/70 bg-surface-2/40 px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-2"
        >
          <Plus size={12} aria-hidden /> Add credits
        </Link>
      </div>

      {buckets.length > 0 && (
        <div className="mt-5">
          <div className="flex items-center gap-1.5">
            <SectionLabel>Spent oldest-first</SectionLabel>
            <span title="When you start a run, credits are drawn from the oldest bucket first.">
              <HelpCircle
                size={11}
                className="text-muted-foreground/70"
                aria-label="When you start a run, credits are drawn from the oldest bucket first."
              />
            </span>
          </div>
          <ul className="mt-2 space-y-1.5">
            {buckets.map((b) => {
              const usedPct =
                b.original > 0
                  ? Math.max(0, Math.min(100, ((b.original - b.remaining) / b.original) * 100))
                  : 0;
              return (
                <li
                  key={b.id}
                  className="rounded-lg border border-border/60 bg-background/40 px-3 py-2"
                >
                  <div className="flex items-baseline justify-between gap-3 text-[12.5px]">
                    <span className="truncate text-foreground">{b.label}</span>
                    <span className="shrink-0 font-mono text-foreground/90">
                      {formatCredits(b.remaining)}
                      <span className="text-muted-foreground"> / {formatCredits(b.original)}</span>
                    </span>
                  </div>
                  <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-muted/60">
                    <div
                      className="h-full rounded-full bg-foreground/40"
                      style={{ width: `${usedPct}%` }}
                      aria-hidden
                    />
                  </div>
                  <div className="mt-1 flex items-center gap-1 text-[10.5px] text-muted-foreground">
                    <span>{formatRelative(b.granted_at)}</span>
                    {b.expires_at && (
                      <>
                        <span aria-hidden>·</span>
                        <span>expires {formatDate(b.expires_at)}</span>
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}

function PlanCard({
  plan,
  status,
  nextRenewal,
  isOpeningPortal,
  onManageBilling,
}: {
  plan: Plan;
  status: SubscriptionStatus;
  nextRenewal?: string | null;
  isOpeningPortal: boolean;
  onManageBilling: () => void;
}) {
  const chip = STATUS_CHIPS[status];
  const renewalLabel = formatDate(nextRenewal);
  return (
    <section
      aria-labelledby="acct-plan"
      className="rounded-xl border border-border/70 bg-surface-1/60 p-5"
    >
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <SectionLabel>Plan</SectionLabel>
          <div className="mt-1 flex items-center gap-2">
            <p id="acct-plan" className="text-base font-semibold text-foreground">
              {plan.name}
            </p>
            {status !== "none" && (
              <span
                className={cn(
                  "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ring-1 ring-inset",
                  chip.className,
                )}
              >
                {chip.label}
              </span>
            )}
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            {plan.price_usd_monthly > 0
              ? `$${plan.price_usd_monthly}/mo · ${formatCredits(plan.credits_per_month)} credits each cycle`
              : "Free tier · 500 credits to start"}
          </p>
          {renewalLabel && status === "active" && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              Next renewal {renewalLabel}
            </p>
          )}
        </div>
        <Button
          variant="outline"
          onClick={onManageBilling}
          disabled={isOpeningPortal}
          className="shrink-0 gap-2"
        >
          {isOpeningPortal ? (
            <Loader2 size={14} className="motion-safe:animate-spin" aria-hidden />
          ) : (
            <CreditCard size={14} aria-hidden />
          )}
          Manage billing
          {!isOpeningPortal && <ExternalLink size={11} className="opacity-60" aria-hidden />}
        </Button>
      </div>
    </section>
  );
}

function TxRow({ tx }: { tx: LedgerTx }) {
  const isCredit = tx.type === "grant" || tx.type === "topup" || tx.type === "refund";
  const sign = isCredit ? "+" : "−";
  const tone = isCredit ? "text-emerald-300" : "text-foreground";
  const typeLabel: Record<LedgerType, string> = {
    grant: "Grant",
    topup: "Top-up",
    spend: "Run",
    refund: "Refund",
    expiry: "Expired",
  };
  return (
    <li className="grid grid-cols-[1fr_auto_auto] items-baseline gap-3 px-4 py-2.5 text-[12.5px] hover:bg-surface-2/30">
      <div className="min-w-0">
        <p className="truncate text-foreground">
          {tx.ref_link ? (
            <Link to={tx.ref_link} className="hover:underline">
              {tx.ref_label}
            </Link>
          ) : (
            tx.ref_label
          )}
        </p>
        <p className="mt-0.5 text-[10.5px] text-muted-foreground">
          {typeLabel[tx.type]}
          <span className="opacity-50"> · </span>
          {formatRelative(tx.created_at)}
        </p>
      </div>
      <span className={cn("font-mono tabular-nums", tone)}>
        {sign}
        {formatCredits(tx.credits)}
      </span>
      <span className="font-mono text-[10.5px] tabular-nums text-muted-foreground">
        {creditsToUsd(tx.credits)}
      </span>
    </li>
  );
}

function TransactionsCard({ transactions }: { transactions: LedgerTx[] }) {
  return (
    <section
      aria-labelledby="acct-tx"
      className="overflow-hidden rounded-xl border border-border/70 bg-surface-1/60"
    >
      <div className="flex items-baseline justify-between border-b border-border/60 px-5 py-4">
        <div>
          <SectionLabel>Recent activity</SectionLabel>
          <p id="acct-tx" className="mt-0.5 text-[13px] text-foreground">
            Last {transactions.length} {transactions.length === 1 ? "entry" : "entries"}
          </p>
        </div>
        <p className="text-[10.5px] text-muted-foreground">Newest first</p>
      </div>

      {transactions.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-10 text-center">
          <div className="rounded-full border border-border/60 bg-surface-2/40 p-3 text-muted-foreground">
            <Inbox size={16} aria-hidden />
          </div>
          <p className="text-[13px] text-foreground">No activity yet.</p>
          <p className="max-w-xs text-[11.5px] text-muted-foreground">
            Start a run from the studio. Every grant, spend, and refund will land here.
          </p>
          <Link
            to="/build"
            className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border/70 bg-surface-2/40 px-3 py-1.5 text-[12px] font-medium text-foreground transition-colors hover:bg-surface-2"
          >
            Open studio
          </Link>
        </div>
      ) : (
        <ul className="divide-y divide-border/40">
          {transactions.map((tx) => (
            <TxRow key={tx.id} tx={tx} />
          ))}
        </ul>
      )}
    </section>
  );
}

function ActionsRow({
  role,
  onLogout,
  onNavigateAdmin,
}: {
  role: "user" | "admin";
  onLogout: () => void;
  onNavigateAdmin?: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Link
        to="/tasks"
        className="inline-flex items-center gap-2 rounded-md border border-border/70 bg-surface-1/60 px-3 py-1.5 text-[12.5px] font-medium text-foreground transition-colors hover:bg-surface-2"
      >
        <Inbox size={13} aria-hidden /> Your runs
      </Link>
      {role === "admin" && onNavigateAdmin && (
        <button
          type="button"
          onClick={onNavigateAdmin}
          className="inline-flex items-center gap-2 rounded-md border border-border/70 bg-surface-1/60 px-3 py-1.5 text-[12.5px] font-medium text-foreground transition-colors hover:bg-surface-2"
        >
          <ShieldCheck size={13} aria-hidden /> Admin panel
        </button>
      )}
      <button
        type="button"
        onClick={onLogout}
        className="ml-auto inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-[12.5px] text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground"
      >
        <LogOut size={13} aria-hidden /> Sign out
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Root

export function AccountView({
  data,
  isOpeningPortal,
  onManageBilling,
  onLogout,
  onNavigateAdmin,
}: AccountViewProps) {
  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-12 sm:py-16">
      <HeaderRow name={data.name} email={data.email} role={data.role} />

      <div className="mt-8 grid gap-5 md:grid-cols-2">
        <BalanceCard balance={data.balance} buckets={data.buckets} />
        <PlanCard
          plan={data.plan}
          status={data.subscriptionStatus}
          nextRenewal={data.nextRenewal}
          isOpeningPortal={isOpeningPortal}
          onManageBilling={onManageBilling}
        />
      </div>

      <div className="mt-5">
        <TransactionsCard transactions={data.transactions} />
      </div>

      <div className="mt-6">
        <ActionsRow
          role={data.role}
          onLogout={onLogout}
          onNavigateAdmin={onNavigateAdmin}
        />
      </div>
    </div>
  );
}
