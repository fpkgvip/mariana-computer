"""Deft Vault — zero-knowledge encrypted secret storage.

The server NEVER decrypts or otherwise inspects vault material. Its only
jobs are:

  • Persist ciphertext + metadata in Supabase under strict RLS.
  • Hand encrypted blobs back to the owner (only) on request.
  • At agent execution time, fetch ciphertext + KDF metadata so the
    sandbox-side worker (which holds the user's unlock key, never the
    API server) can decrypt and inject env vars right before exec.

All cryptography is performed client-side; this module is plumbing.
"""

from .store import (  # noqa: F401
    VaultError,
    VaultExists,
    VaultNotFound,
    SecretExists,
    SecretNotFound,
    create_vault,
    get_vault,
    delete_vault,
    list_secrets,
    create_secret,
    update_secret,
    delete_secret,
)
from .router import build_vault_router  # noqa: F401
