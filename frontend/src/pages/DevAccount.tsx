/**
 * Dev-only Account preview.
 *
 * Renders AccountView with deterministic mock data so we can iterate on
 * visuals at any viewport without auth.  Gated on import.meta.env.DEV in
 * App.tsx.
 *
 * ?mode=plus | empty | admin | trial | pastdue
 */
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import {
  AccountView,
  type AccountData,
  type CreditBucket,
  type LedgerTx,
} from "@/components/deft/account/AccountView";
import { BuyCreditsDialog } from "@/components/deft/account/BuyCreditsDialog";

type Mode = "plus" | "empty" | "admin" | "trial" | "pastdue";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

const PLAN_STANDARD = {
  id: "standard",
  name: "Standard",
  price_usd_monthly: 99,
  credits_per_month: 13_000,
};

const PLAN_FREE = {
  id: "free",
  name: "Free",
  price_usd_monthly: 0,
  credits_per_month: 500,
};

const BUCKETS: CreditBucket[] = [
  {
    id: "b1",
    source: "grant",
    label: "Standard plan grant",
    remaining: 3_950,
    original: 13_000,
    granted_at: new Date(Date.now() - 12 * DAY).toISOString(),
    expires_at: new Date(Date.now() + 18 * DAY).toISOString(),
  },
  {
    id: "b2",
    source: "topup",
    label: "Top-up · 5,000",
    remaining: 5_000,
    original: 5_000,
    granted_at: new Date(Date.now() - 3 * DAY).toISOString(),
  },
];

function mkTx(
  id: string,
  type: LedgerTx["type"],
  credits: number,
  ref: string,
  ago: number,
  link?: string,
): LedgerTx {
  return {
    id,
    type,
    credits,
    ref_label: ref,
    ref_link: link ?? null,
    created_at: new Date(Date.now() - ago).toISOString(),
  };
}

const TX: LedgerTx[] = [
  mkTx("t1", "spend", 312, "Habit tracker run", 4 * 60_000, "/tasks/tsk_dev_1"),
  mkTx("t2", "spend", 188, "Markdown editor run", 2 * HOUR, "/tasks/tsk_dev_2"),
  mkTx("t3", "topup", 5_000, "Top-up · $50", 3 * DAY),
  mkTx("t4", "spend", 84, "Telegram bot run", 1 * DAY),
  mkTx("t5", "refund", 84, "Run failed before deploy", 1 * DAY - 60_000, "/tasks/tsk_dev_3"),
  mkTx("t6", "spend", 220, "Payroll API landing", 3 * DAY),
  mkTx("t7", "spend", 410, "Notion clone — pages tree", 5 * DAY),
  mkTx("t8", "spend", 95, "Resume checker", 6 * DAY),
  mkTx("t9", "grant", 13_000, "Standard plan grant", 12 * DAY),
  mkTx("t10", "spend", 740, "Grocery list with auth", 14 * DAY),
];

export default function DevAccount() {
  // Hooks must run unconditionally on every render (rules-of-hooks).
  // The DEV-only gate below only suppresses the rendered JSX.
  const [params, setParams] = useSearchParams();
  const mode = (params.get("mode") as Mode | null) ?? "plus";
  const [opening, setOpening] = useState(false);
  const [buyOpen, setBuyOpen] = useState(false);
  const isDev = import.meta.env.DEV;

  const goto = (m: Mode) => {
    const next = new URLSearchParams(params);
    next.set("mode", m);
    setParams(next, { replace: true });
  };

  const data = useMemo<AccountData>(() => {
    if (mode === "empty") {
      return {
        name: "Sam Reyes",
        email: "sam@example.com",
        role: "user",
        plan: PLAN_FREE,
        subscriptionStatus: "none",
        balance: 500,
        buckets: [
          {
            id: "b0",
            source: "grant",
            label: "Free trial grant",
            remaining: 500,
            original: 500,
            granted_at: new Date().toISOString(),
          },
        ],
        transactions: [],
      };
    }
    if (mode === "admin") {
      return {
        name: "Sam Reyes",
        email: "sam@deft.computer",
        role: "admin",
        plan: PLAN_STANDARD,
        subscriptionStatus: "active",
        nextRenewal: new Date(Date.now() + 18 * DAY).toISOString(),
        balance: 8_950,
        buckets: BUCKETS,
        transactions: TX,
      };
    }
    if (mode === "trial") {
      return {
        name: "Sam Reyes",
        email: "sam@example.com",
        role: "user",
        plan: PLAN_STANDARD,
        subscriptionStatus: "trialing",
        nextRenewal: new Date(Date.now() + 5 * DAY).toISOString(),
        balance: 12_400,
        buckets: BUCKETS,
        transactions: TX.slice(0, 4),
      };
    }
    if (mode === "pastdue") {
      return {
        name: "Sam Reyes",
        email: "sam@example.com",
        role: "user",
        plan: PLAN_STANDARD,
        subscriptionStatus: "past_due",
        nextRenewal: new Date(Date.now() - 2 * DAY).toISOString(),
        balance: 110,
        buckets: BUCKETS.map((b) => ({ ...b, remaining: Math.min(b.remaining, 110) })),
        transactions: TX.slice(0, 6),
      };
    }
    return {
      name: "Sam Reyes",
      email: "sam@example.com",
      role: "user",
      plan: PLAN_STANDARD,
      subscriptionStatus: "active",
      nextRenewal: new Date(Date.now() + 18 * DAY).toISOString(),
      balance: 8_950,
      buckets: BUCKETS,
      transactions: TX,
    };
  }, [mode]);

  if (!isDev) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-foreground">
        <p className="text-sm text-muted-foreground">Dev preview disabled in production.</p>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <Navbar />
      <div className="mt-16 border-b border-border/60 bg-surface-1/40 px-3 py-2">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <span className="font-mono uppercase tracking-[0.16em]">/dev/account</span>
          <span aria-hidden>·</span>
          <ModeButton current={mode} value="plus" onClick={() => goto("plus")} />
          <ModeButton current={mode} value="empty" onClick={() => goto("empty")} />
          <ModeButton current={mode} value="admin" onClick={() => goto("admin")} />
          <ModeButton current={mode} value="trial" onClick={() => goto("trial")} />
          <ModeButton current={mode} value="pastdue" onClick={() => goto("pastdue")} />
        </div>
      </div>
      <AccountView
        data={data}
        isOpeningPortal={opening}
        onManageBilling={() => {
          setOpening(true);
          window.setTimeout(() => setOpening(false), 1200);
        }}
        onAddCredits={() => setBuyOpen(true)}
        onLogout={() => alert("(dev) sign out")}
        onNavigateAdmin={data.role === "admin" ? () => alert("(dev) admin") : undefined}
      />
      <BuyCreditsDialog
        open={buyOpen}
        onOpenChange={setBuyOpen}
        currentBalance={data.balance}
        onPurchase={async () => {
          // dev: simulate latency, log only.
          await new Promise((r) => window.setTimeout(r, 600));
          // eslint-disable-next-line no-console
          console.log("(dev) BuyCreditsDialog purchase");
        }}
      />
    </div>
  );
}

function ModeButton({
  current,
  value,
  onClick,
}: {
  current: Mode;
  value: Mode;
  onClick: () => void;
}) {
  const active = current === value;
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "rounded px-2 py-0.5 font-mono uppercase tracking-[0.12em] transition-colors " +
        (active
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground hover:bg-secondary hover:text-foreground")
      }
    >
      {value}
    </button>
  );
}
