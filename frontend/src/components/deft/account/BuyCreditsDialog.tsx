/**
 * BuyCreditsDialog — Add-credits flow as an in-place modal, replacing the
 * old full-page navigation to /checkout. Surfaces three top-up packs
 * (small / medium / large) at 1c = $0.01, with a calm Stripe disclosure
 * and a single primary action that hits /api/billing/create-checkout for
 * the selected pack.
 *
 * The modal is intentionally pure-presentational: it accepts an
 * `onPurchase(packId)` callback so the parent owns the Supabase session
 * and the popup-window dance required to keep Safari's user gesture
 * alive (BUG-FE-121, see Pricing.tsx).
 */
import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ArrowRight, Loader2, Lock } from "lucide-react";

export interface CreditPack {
  id: string;
  name: string;
  /** USD price */
  price: number;
  /** Credits awarded for this pack */
  credits: number;
  /** True for the highlighted middle option */
  recommended?: boolean;
}

export const DEFAULT_PACKS: CreditPack[] = [
  { id: "topup_small", name: "Small pack", price: 10, credits: 1_000 },
  { id: "topup_medium", name: "Medium pack", price: 30, credits: 3_000, recommended: true },
  { id: "topup_large", name: "Large pack", price: 150, credits: 15_000 },
];

export interface BuyCreditsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Caller-supplied list of packs (defaults to DEFAULT_PACKS). */
  packs?: CreditPack[];
  /** Default selection. Defaults to the recommended pack, falling back to first. */
  defaultPackId?: string;
  /** Stripe-bound purchase handler. Should return when redirect has begun. */
  onPurchase: (packId: string) => Promise<void> | void;
  /** When true the primary button shows a spinner. */
  isPurchasing?: boolean;
  /** Optional current balance for context. */
  currentBalance?: number;
}

export function BuyCreditsDialog({
  open,
  onOpenChange,
  packs = DEFAULT_PACKS,
  defaultPackId,
  onPurchase,
  isPurchasing = false,
  currentBalance,
}: BuyCreditsDialogProps) {
  const recommended = packs.find((p) => p.recommended) ?? packs[0];
  const initial = defaultPackId ?? recommended?.id ?? packs[0]?.id;
  const [selectedId, setSelectedId] = useState<string | undefined>(initial);

  // When the dialog reopens, snap selection back to the default pack
  useEffect(() => {
    if (open) setSelectedId(initial);
  }, [open, initial]);

  const selected = packs.find((p) => p.id === selectedId) ?? recommended;

  const handleConfirm = async () => {
    if (!selected || isPurchasing) return;
    await onPurchase(selected.id);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>Add credits</DialogTitle>
          <DialogDescription>
            Top-ups apply instantly and don&rsquo;t expire while your subscription is active.
            {typeof currentBalance === "number" && (
              <>
                {" "}
                Current balance:{" "}
                <span className="font-mono text-foreground">
                  {currentBalance.toLocaleString()}
                </span>{" "}
                credits.
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <fieldset
          className="grid gap-2"
          aria-label="Credit packs"
          disabled={isPurchasing}
        >
          {packs.map((pack) => {
            const isSelected = pack.id === selectedId;
            return (
              <label
                key={pack.id}
                className={[
                  "flex cursor-pointer items-center gap-4 rounded-lg border px-4 py-3 transition-all",
                  isSelected
                    ? "border-accent/60 bg-accent/[0.06] ring-1 ring-accent/30"
                    : "border-border/70 bg-surface-1/50 hover:border-border",
                ].join(" ")}
              >
                <input
                  type="radio"
                  name="credit-pack"
                  value={pack.id}
                  checked={isSelected}
                  onChange={() => setSelectedId(pack.id)}
                  className="sr-only"
                />
                <span
                  aria-hidden
                  className={[
                    "grid size-4 shrink-0 place-items-center rounded-full border transition-colors",
                    isSelected ? "border-accent bg-accent" : "border-border bg-background",
                  ].join(" ")}
                >
                  {isSelected && <span className="size-1.5 rounded-full bg-background" />}
                </span>
                <div className="flex-1">
                  <div className="flex items-baseline justify-between gap-3">
                    <p className="text-[14px] font-medium text-foreground">
                      {pack.name}
                      {pack.recommended && (
                        <span className="ml-2 rounded-full border border-accent/40 bg-accent/[0.08] px-1.5 py-0.5 text-[10.5px] font-medium tracking-[0.01em] text-accent-strong">
                          Recommended
                        </span>
                      )}
                    </p>
                    <p className="font-mono text-[14px] font-medium text-foreground">
                      ${pack.price}
                    </p>
                  </div>
                  <p className="mt-0.5 text-[12px] text-muted-foreground">
                    +{pack.credits.toLocaleString()} credits &middot; ≈ ${pack.credits / 100} of compute
                  </p>
                </div>
              </label>
            );
          })}
        </fieldset>

        <p className="flex items-start gap-2 rounded-md border border-border/60 bg-surface-1/40 p-3 text-[11.5px] leading-[1.55] text-muted-foreground">
          <Lock size={12} className="mt-0.5 shrink-0 text-muted-foreground/80" aria-hidden />
          <span>
            Payment is processed by Stripe. We never see your card. After a
            successful charge you&rsquo;re returned to your account with the new
            credits already in your balance.
          </span>
        </p>

        <DialogFooter>
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            disabled={isPurchasing}
            className="rounded-md border border-border/70 bg-surface-2 px-4 py-2 text-[13px] font-medium text-foreground transition-colors hover:bg-surface-3 disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={!selected || isPurchasing}
            className="inline-flex items-center justify-center gap-1.5 rounded-md bg-accent px-4 py-2 text-[13px] font-medium text-accent-foreground shadow-[0_4px_16px_-6px_hsl(var(--accent)/0.6)] transition-all hover:brightness-110 disabled:opacity-60"
          >
            {isPurchasing ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <>
                Buy {selected ? `$${selected.price}` : ""} <ArrowRight size={13} />
              </>
            )}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
