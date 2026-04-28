-- =====================================================================
-- Deft v1.0 — Phase 5: Zero-knowledge Vault
-- =====================================================================
-- Architecture:
--   • All key material is derived client-side from a user passphrase.
--   • Server stores ciphertext only — it cannot decrypt.
--   • One vault per user (1:1).  Each secret is encrypted under K_master,
--     where K_master is itself encrypted with a key derived from the
--     passphrase via Argon2id (m=64MiB, t=3, p=4).
--   • A second copy of K_master is sealed under a Recovery Key derived
--     from a 24-character base32 recovery code shown once to the user.
--
-- Cryptography (enforced by the client; the server cannot verify):
--   • Argon2id parameters fixed at: memory=65536KiB, iterations=3, parallelism=4.
--   • AES-256-GCM with 96-bit (12-byte) random IV per encryption.
--   • All blobs are stored as bytea; their lengths are constrained to
--     the algorithm-defined ranges.
--
-- The server never logs or returns plaintext, key material, or
-- intermediate values.  RLS restricts every row to its owner.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. user_vaults — one row per user (the locked safe)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_vaults (
  user_id              uuid        PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,

  -- Argon2id KDF parameters (mirrored so the client knows how to re-derive)
  kdf_algorithm        text        NOT NULL DEFAULT 'argon2id'
                                    CHECK (kdf_algorithm = 'argon2id'),
  kdf_memory_kib       integer     NOT NULL DEFAULT 65536
                                    CHECK (kdf_memory_kib BETWEEN 16384 AND 1048576),
  kdf_iterations       integer     NOT NULL DEFAULT 3
                                    CHECK (kdf_iterations BETWEEN 1 AND 16),
  kdf_parallelism      integer     NOT NULL DEFAULT 4
                                    CHECK (kdf_parallelism BETWEEN 1 AND 16),

  -- Salt for passphrase-derived KEK (16 bytes random per vault).
  passphrase_salt      bytea       NOT NULL
                                    CHECK (octet_length(passphrase_salt) = 16),

  -- K_master sealed under K_passphrase via AES-256-GCM:
  --   passphrase_iv  : 12-byte IV
  --   passphrase_blob: ciphertext + 16-byte GCM tag (=> 48 bytes for a 32-byte K_master)
  passphrase_iv        bytea       NOT NULL
                                    CHECK (octet_length(passphrase_iv) = 12),
  passphrase_blob      bytea       NOT NULL
                                    CHECK (octet_length(passphrase_blob) BETWEEN 16 AND 96),

  -- Salt for recovery-code-derived KEK (16 bytes random per vault).
  recovery_salt        bytea       NOT NULL
                                    CHECK (octet_length(recovery_salt) = 16),

  -- K_master sealed under K_recovery via AES-256-GCM:
  recovery_iv          bytea       NOT NULL
                                    CHECK (octet_length(recovery_iv) = 12),
  recovery_blob        bytea       NOT NULL
                                    CHECK (octet_length(recovery_blob) BETWEEN 16 AND 96),

  -- A short, random "verifier": 32 bytes of random plaintext encrypted under
  -- K_master.  After unlock the client decrypts this to confirm the wrap is
  -- valid (and to fail-fast on a wrong passphrase).
  verifier_iv          bytea       NOT NULL
                                    CHECK (octet_length(verifier_iv) = 12),
  verifier_blob        bytea       NOT NULL
                                    CHECK (octet_length(verifier_blob) BETWEEN 16 AND 128),

  created_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at           timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE public.user_vaults ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "user_vaults_owner_select" ON public.user_vaults;
CREATE POLICY "user_vaults_owner_select"
  ON public.user_vaults FOR SELECT
  USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "user_vaults_owner_insert" ON public.user_vaults;
CREATE POLICY "user_vaults_owner_insert"
  ON public.user_vaults FOR INSERT
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "user_vaults_owner_update" ON public.user_vaults;
CREATE POLICY "user_vaults_owner_update"
  ON public.user_vaults FOR UPDATE
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "user_vaults_owner_delete" ON public.user_vaults;
CREATE POLICY "user_vaults_owner_delete"
  ON public.user_vaults FOR DELETE
  USING (auth.uid() = user_id);

-- ---------------------------------------------------------------------
-- 2. vault_secrets — individual encrypted secrets
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.vault_secrets (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,

  -- Plaintext name (e.g. "OPENAI_API_KEY").  Used as the sentinel key
  -- (`$OPENAI_API_KEY`) and for env var injection at execution time.
  -- Constrained to the conventional shell env-var grammar so that a
  -- malicious key name cannot break out of the substitution context.
  name            text        NOT NULL
                              CHECK (name ~ '^[A-Z][A-Z0-9_]{0,63}$'),

  -- Optional human-readable description (plaintext, never sent to LLMs).
  description     text,

  -- AES-256-GCM under K_master, with a fresh 12-byte IV per write.
  -- value_blob includes the 16-byte authentication tag.
  value_iv        bytea       NOT NULL
                              CHECK (octet_length(value_iv) = 12),
  value_blob      bytea       NOT NULL
                              CHECK (octet_length(value_blob) BETWEEN 16 AND 65552),

  -- Last 4 plaintext characters (encrypted) so the UI can show "····abcd"
  -- after a successful unlock without ever transmitting the full secret.
  -- Encrypted independently with its own IV under K_master.
  preview_iv      bytea       NOT NULL
                              CHECK (octet_length(preview_iv) = 12),
  preview_blob    bytea       NOT NULL
                              CHECK (octet_length(preview_blob) BETWEEN 16 AND 64),

  created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),

  UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_vault_secrets_user
  ON public.vault_secrets (user_id, name);

ALTER TABLE public.vault_secrets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "vault_secrets_owner_select" ON public.vault_secrets;
CREATE POLICY "vault_secrets_owner_select"
  ON public.vault_secrets FOR SELECT
  USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "vault_secrets_owner_insert" ON public.vault_secrets;
CREATE POLICY "vault_secrets_owner_insert"
  ON public.vault_secrets FOR INSERT
  WITH CHECK (
    auth.uid() = user_id
    AND EXISTS (SELECT 1 FROM public.user_vaults v WHERE v.user_id = auth.uid())
  );

DROP POLICY IF EXISTS "vault_secrets_owner_update" ON public.vault_secrets;
CREATE POLICY "vault_secrets_owner_update"
  ON public.vault_secrets FOR UPDATE
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "vault_secrets_owner_delete" ON public.vault_secrets;
CREATE POLICY "vault_secrets_owner_delete"
  ON public.vault_secrets FOR DELETE
  USING (auth.uid() = user_id);

-- ---------------------------------------------------------------------
-- 3. updated_at trigger (shared between both tables)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
  NEW.updated_at = clock_timestamp();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_user_vaults_touch ON public.user_vaults;
CREATE TRIGGER trg_user_vaults_touch
  BEFORE UPDATE ON public.user_vaults
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

DROP TRIGGER IF EXISTS trg_vault_secrets_touch ON public.vault_secrets;
CREATE TRIGGER trg_vault_secrets_touch
  BEFORE UPDATE ON public.vault_secrets
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- ---------------------------------------------------------------------
-- 4. has_vault helper view
-- ---------------------------------------------------------------------
-- Lets the frontend cheaply ask "does this user have a vault yet?" without
-- pulling any blobs or metadata.
CREATE OR REPLACE VIEW public.vault_status
WITH (security_invoker = true) AS
SELECT
  v.user_id,
  TRUE                                                       AS has_vault,
  v.created_at,
  (SELECT count(*) FROM public.vault_secrets s WHERE s.user_id = v.user_id) AS secret_count
FROM public.user_vaults v;

GRANT SELECT ON public.vault_status TO authenticated;

-- =====================================================================
-- End of 003_deft_vault.sql
-- =====================================================================
