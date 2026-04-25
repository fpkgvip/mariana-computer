/**
 * VaultHeader — page header + on-device guarantee strip.
 *
 * Calm copy, no hype.  The strip restates the core promise that defines
 * Vault: encryption is local; the agent only ever sees a sentinel.
 */
import { Lock, ShieldCheck, EyeOff } from "lucide-react";

export function VaultHeader() {
  return (
    <header className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground sm:text-[28px]">
          Vault
        </h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Store API keys and other secrets. Reference them in prompts as{" "}
          <code className="rounded bg-muted/70 px-1 font-mono text-[11.5px] text-foreground">
            $KEY_NAME
          </code>
          . The agent only ever sees the sentinel; the real value is injected
          into the sandbox at run time and redacted from output.
        </p>
      </div>

      <ul
        aria-label="Vault guarantees"
        className="grid gap-2 rounded-xl border border-border/70 bg-surface-1/60 p-4 text-[12.5px] text-foreground sm:grid-cols-3"
      >
        <Guarantee
          icon={<Lock size={13} />}
          title="Encrypted on your device"
          body="Argon2id passphrase, AES-GCM ciphertext."
        />
        <Guarantee
          icon={<EyeOff size={13} />}
          title="Sentinel in the prompt"
          body="The model sees $KEY, not the value."
        />
        <Guarantee
          icon={<ShieldCheck size={13} />}
          title="Redacted on the way out"
          body="Stdout, logs, and artifacts get scrubbed."
        />
      </ul>
    </header>
  );
}

function Guarantee({
  icon,
  title,
  body,
}: {
  icon: React.ReactNode;
  title: string;
  body: string;
}) {
  return (
    <li className="flex items-start gap-2">
      <span className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-surface-2/60 text-foreground/80">
        {icon}
      </span>
      <span className="min-w-0">
        <span className="block text-[12.5px] font-medium text-foreground">{title}</span>
        <span className="block text-[11.5px] text-muted-foreground">{body}</span>
      </span>
    </li>
  );
}
