/**
 * /vault — Deft Vault page.
 *
 * Three states:
 *   1. No vault row on server → setup wizard.
 *   2. Vault exists, but locked → unlock dialog.
 *   3. Vault unlocked → secrets table + lock/destroy controls.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useAuth } from "@/contexts/AuthContext";
import { useVault } from "@/hooks/useVault";
import { VaultSetupWizard } from "@/components/deft/VaultSetupWizard";
import { VaultUnlockDialog } from "@/components/deft/VaultUnlockDialog";
import { SecretsTable } from "@/components/deft/SecretsTable";
import { Lock, Loader2, ShieldCheck, Trash2 } from "lucide-react";
import { toast } from "sonner";

export default function VaultPage() {
  const { user, loading: authLoading } = useAuth();
  const navigate = useNavigate();
  const vault = useVault();
  const [confirmDestroy, setConfirmDestroy] = useState(false);
  // We keep the wizard mounted until the user explicitly clicks "Open vault"
  // (onDone). This ensures the recovery-code reveal step is always visible
  // immediately after setup, not whisked away by a state transition.
  const [setupInProgress, setSetupInProgress] = useState(false);

  // Whenever there is no vault on the server, we are implicitly in setup mode.
  useEffect(() => {
    if (vault.loaded && !vault.exists) setSetupInProgress(true);
  }, [vault.loaded, vault.exists]);

  // Auth gate (matches the ProtectedRoute pattern used on /build /account etc).
  if (!authLoading && !user) {
    navigate("/login", { replace: true });
    return null;
  }

  const handleDestroy = async () => {
    try {
      await vault.destroyVault();
      toast.success("Vault deleted.");
      setConfirmDestroy(false);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not delete vault.");
    }
  };

  let body: JSX.Element;
  if (!vault.loaded || authLoading) {
    body = (
      <div className="flex items-center justify-center py-20 text-muted-foreground">
        <Loader2 size={20} className="mr-2 animate-spin" />
        Loading vault…
      </div>
    );
  } else if (!vault.exists || setupInProgress) {
    body = <VaultSetupWizard onDone={() => setSetupInProgress(false)} />;
  } else if (!vault.unlocked) {
    body = <VaultUnlockDialog />;
  } else {
    body = (
      <div className="space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3">
          <div className="flex items-center gap-2 text-sm">
            <ShieldCheck size={16} className="text-primary" />
            <span className="font-medium text-foreground">Vault unlocked</span>
            <span className="text-xs text-muted-foreground">
              auto-locks after 30 minutes of inactivity
            </span>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={vault.lock}>
              <Lock size={14} className="mr-1.5" />
              Lock now
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setConfirmDestroy(true)}
              className="text-red-400 hover:text-red-300"
            >
              <Trash2 size={14} className="mr-1.5" />
              Delete vault
            </Button>
          </div>
        </div>
        <SecretsTable />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <main className="mx-auto max-w-3xl px-6 pt-24 pb-16">
        <header className="mb-6">
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">Vault</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Encrypted in your browser. Reference secrets in prompts as <code className="rounded bg-muted px-1">$KEY_NAME</code>;
            we substitute them only inside the agent sandbox and redact them from output.
          </p>
        </header>

        {vault.loadError && (
          <div className="mb-4 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
            {vault.loadError}
          </div>
        )}

        {body}
      </main>

      <AlertDialog open={confirmDestroy} onOpenChange={(v) => !v && setConfirmDestroy(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete your vault?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently destroys the vault and all secrets stored in it. You will need
              to set up a new vault and re-add every key. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDestroy}
              className="bg-red-500 text-white hover:bg-red-600"
            >
              Delete vault
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Footer />
    </div>
  );
}
