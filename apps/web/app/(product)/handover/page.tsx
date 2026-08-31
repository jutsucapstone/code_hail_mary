"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";

import { Container } from "@/components/site/section";
import { FormError } from "@/components/pilot/submit-button";
import { Field } from "@/components/pilot/field";
import { api } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * The knowledge-transfer entry — where a KT ID becomes a workspace.
 *
 * The input takes the ID exactly as the administrator shared it. Every decision about
 * it is the server's: whether it exists, whether it is yours, whether it is still
 * valid. A revoked or expired package answers with the precise sentence to show (§39),
 * and everything else — a typo, a foreign organisation's code, a package bound to
 * somebody else — is the same "no package matches" answer, because a KT ID must never
 * confirm anything to whoever happens to hold it (§15).
 *
 * This page replaced the Handover Studio stub: opening a package is live; the cited
 * leaver-pack *generator* still needs the knowledge graph and stays honestly absent.
 */
export default function KnowledgeTransferEntryPage() {
  const router = useRouter();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const open = useMutation({
    mutationFn: (ktCode: string) => api.ktClaim(ktCode),
    onSuccess: (pkg) => {
      router.push(`/kt/${encodeURIComponent(pkg.kt_code)}`);
    },
    onError: (mutationError: unknown) => {
      setError(classifyApiError(mutationError).message);
    },
  });

  function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = code.trim();
    if (trimmed) {
      setError(null);
      open.mutate(trimmed);
    }
  }

  return (
    <Container className="py-16 lg:py-24">
      <div className="max-w-2xl">
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          Knowledge transfer
        </p>
        <h1 className="display mt-5 text-4xl font-semibold sm:text-5xl">
          Have a KT ID from your administrator?
        </h1>
        <p className="mt-6 max-w-prose text-pretty text-base leading-relaxed text-muted-foreground">
          A knowledge-transfer package gives you a focused workspace over one
          colleague&apos;s context — scoped, time-limited, and addressed to you. Enter
          the ID you were given to open it.
        </p>

        <form onSubmit={onSubmit} className="mt-10 flex flex-col gap-4 sm:flex-row sm:items-end">
          <Field
            id="kt-code"
            name="kt_code"
            label="KT ID"
            placeholder="KT-JUTSU-XXXXXXXX"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            required
            maxLength={24}
            className="flex-1 font-mono"
          />
          <button
            type="submit"
            disabled={open.isPending}
            aria-busy={open.isPending}
            className="h-12 rounded-xl bg-brand px-6 text-[0.9375rem] font-semibold text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60 sm:w-40"
          >
            {open.isPending ? "Opening…" : "Open KT"}
          </button>
        </form>

        {error ? (
          <div className="mt-4">
            <FormError message={error} />
          </div>
        ) : null}

        <p className="mt-8 max-w-prose text-pretty text-xs leading-relaxed text-muted-foreground">
          A KT ID is not a key to a database. What you see inside the workspace is
          bounded by the package&apos;s scope and by what your own account is authorised
          to read — and your administrator can revoke it at any time.
        </p>
      </div>
    </Container>
  );
}
