/**
 * Dev-only preview for the P13 list-state primitives.
 *
 * Renders every variant of LoadingRows / EmptyState / ErrorState side by
 * side so we can verify spacing, copy, and tone at multiple viewport
 * widths in a single screenshot. Gated on import.meta.env.DEV.
 */
import { Navbar } from "@/components/Navbar";
import {
  EmptyState,
  ErrorState,
  InlineLoading,
  LoadingRows,
} from "@/components/deft/states";
import { FolderOpen, Inbox, KeyRound } from "lucide-react";

export default function DevStates() {
  if (!import.meta.env.DEV) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-foreground">
        <p className="text-sm text-muted-foreground">Dev preview disabled in production.</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <Navbar />
      <div className="mt-16 border-b border-border/60 bg-surface-1/40 px-3 py-2">
        <div className="mx-auto flex max-w-[1280px] items-center gap-2 text-[11px] text-muted-foreground">
          <span className="font-mono uppercase tracking-[0.16em]">/dev/states</span>
          <span aria-hidden>·</span>
          <span>list state primitives</span>
        </div>
      </div>

      <main className="mx-auto max-w-[1280px] space-y-12 px-6 py-10">
        <Section title="Loading">
          <Cell label="LoadingRows · 3 rows · default">
            <LoadingRows count={3} />
          </Cell>
          <Cell label="LoadingRows · 4 rows · bare (sidebar)">
            <LoadingRows count={4} rowClassName="h-12" bare />
          </Cell>
          <Cell label="InlineLoading">
            <InlineLoading />
          </Cell>
        </Section>

        <Section title="Empty">
          <Cell label="EmptyState · default · with action">
            <EmptyState
              icon={<Inbox size={20} aria-hidden />}
              title="No tasks yet"
              description="Kick one off from Research to get started."
              action={
                <button
                  type="button"
                  className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
                >
                  Start a task
                </button>
              }
            />
          </Cell>
          <Cell label="EmptyState · filtered · no action">
            <EmptyState
              icon={<Inbox size={20} aria-hidden />}
              filtered
              title="No matches"
              description="Try a different filter."
            />
          </Cell>
          <Cell label="EmptyState · dense (sidebar)">
            <div className="w-60 rounded-md border border-border bg-[hsl(var(--sidebar-background))]">
              <EmptyState
                dense
                icon={<FolderOpen size={20} aria-hidden />}
                title="No runs yet"
                description="Start your first run from the prompt bar."
              />
            </div>
          </Cell>
        </Section>

        <Section title="Error">
          <Cell label="ErrorState · muted · with retry + request id">
            <ErrorState
              title="Could not load tasks"
              message="Network error reaching the agent service."
              requestId="req_a1b2c3d4e5f60718"
              onRetry={() => undefined}
            />
          </Cell>
          <Cell label="ErrorState · retrying">
            <ErrorState
              title="Could not load tasks"
              message="Network error reaching the agent service."
              requestId="req_a1b2c3d4e5f60718"
              onRetry={() => undefined}
              retrying
            />
          </Cell>
          <Cell label="ErrorState · destructive (mutation failed)">
            <ErrorState
              tone="destructive"
              title="Delete failed"
              message="The run is currently active. Cancel it first, then delete."
              onRetry={() => undefined}
            />
          </Cell>
          <Cell label="ErrorState · dense (sidebar)">
            <div className="w-60 rounded-md border border-border bg-[hsl(var(--sidebar-background))] p-2">
              <ErrorState
                dense
                title="Could not load projects"
                message="Network error."
                requestId="req_72e0d3"
                onRetry={() => undefined}
              />
            </div>
          </Cell>
          <Cell label="ErrorState · with secondary action">
            <ErrorState
              title="Could not unlock vault"
              message="The passphrase you entered is incorrect."
              onRetry={() => undefined}
              secondary={
                <a
                  href="#"
                  className="text-[11px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                >
                  Use recovery phrase
                </a>
              }
            />
          </Cell>
          <Cell label="ErrorState · vault unavailable">
            <ErrorState
              title="Vault unavailable"
              message="We could not reach the secret store. Your secrets are safe — try again in a moment."
              requestId="req_vault_4f"
              onRetry={() => undefined}
            />
          </Cell>
        </Section>

        <Section title="Composed examples">
          <Cell label="Sidebar list · loading">
            <SidebarMock>
              <LoadingRows count={4} rowClassName="h-12" bare />
            </SidebarMock>
          </Cell>
          <Cell label="Sidebar list · empty (filtered)">
            <SidebarMock>
              <EmptyState
                dense
                filtered
                icon={<FolderOpen size={20} aria-hidden />}
                title="No matches"
                description="Try a different keyword."
              />
            </SidebarMock>
          </Cell>
          <Cell label="Sidebar list · error">
            <SidebarMock>
              <ErrorState
                dense
                title="Could not load projects"
                message="Network error."
                requestId="req_72e0d3"
                onRetry={() => undefined}
              />
            </SidebarMock>
          </Cell>
          <Cell label="Vault · empty">
            <EmptyState
              icon={<KeyRound size={20} aria-hidden />}
              title="No secrets yet"
              description="Add a secret to reference it in any prompt as a $KEY sentinel."
              action={
                <button
                  type="button"
                  className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
                >
                  Add secret
                </button>
              }
            />
          </Cell>
        </Section>
      </main>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="mb-4 font-mono text-[11px] uppercase tracking-[0.16em] text-muted-foreground">
        {title}
      </h2>
      <div className="grid gap-6 md:grid-cols-2">{children}</div>
    </section>
  );
}

function Cell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-2 text-[11px] text-muted-foreground">{label}</p>
      <div>{children}</div>
    </div>
  );
}

function SidebarMock({ children }: { children: React.ReactNode }) {
  return (
    <div className="w-60 rounded-md border border-border bg-[hsl(var(--sidebar-background))] p-2">
      {children}
    </div>
  );
}
