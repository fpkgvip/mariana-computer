/**
 * pageHead — tiny per-route document head updater.
 *
 * react-helmet pulls in async store machinery we don't need on a 6-screen
 * marketing site. This hook just sets document.title and the meta tags
 * declared by usePageHead({...}). On unmount it restores the previous
 * values so that pages without a usePageHead() call don't leak the
 * previous route's title into the next render.
 *
 * SEO note: SPAs without server-rendered <title> still benefit because
 * Googlebot now executes JS, and social-card crawlers (Facebook/Twitter)
 * fall back to the static index.html OG tags when they can't render JS.
 * That's why we ALSO keep canonical OG tags in index.html as a baseline.
 */
import { useEffect } from "react";
import { BRAND } from "@/lib/brand";

export interface PageHead {
  /** Page-specific title; will be suffixed with " — {BRAND.name}". */
  title: string;
  /** Optional meta description. Falls back to the index.html default. */
  description?: string;
  /** Optional canonical URL path (e.g. "/pricing"). Default: current pathname. */
  path?: string;
  /** Set to false to suppress the brand suffix (used on the home page). */
  suffixBrand?: boolean;
}

const DEFAULT_DESCRIPTION =
  "Deft is the AI developer that doesn't leave you debugging. It runs your app in a real browser, watches its own output, and fixes its own mistakes.";

function setMeta(selector: string, content: string) {
  let el = document.head.querySelector<HTMLMetaElement>(selector);
  if (!el) {
    el = document.createElement("meta");
    const isProperty = selector.includes("property=");
    const name = selector.match(/=['"]([^'"]+)['"]/)?.[1] ?? "";
    if (isProperty) el.setAttribute("property", name);
    else el.setAttribute("name", name);
    document.head.appendChild(el);
  }
  el.setAttribute("content", content);
}

function setCanonical(href: string) {
  let el = document.head.querySelector<HTMLLinkElement>("link[rel='canonical']");
  if (!el) {
    el = document.createElement("link");
    el.setAttribute("rel", "canonical");
    document.head.appendChild(el);
  }
  el.setAttribute("href", href);
}

export function usePageHead({ title, description, path, suffixBrand = true }: PageHead) {
  useEffect(() => {
    const prev = {
      title: document.title,
      description: document.head.querySelector<HTMLMetaElement>("meta[name='description']")?.getAttribute("content") ?? "",
      ogTitle: document.head.querySelector<HTMLMetaElement>("meta[property='og:title']")?.getAttribute("content") ?? "",
      ogDesc: document.head.querySelector<HTMLMetaElement>("meta[property='og:description']")?.getAttribute("content") ?? "",
      twTitle: document.head.querySelector<HTMLMetaElement>("meta[name='twitter:title']")?.getAttribute("content") ?? "",
      twDesc: document.head.querySelector<HTMLMetaElement>("meta[name='twitter:description']")?.getAttribute("content") ?? "",
      canonical: document.head.querySelector<HTMLLinkElement>("link[rel='canonical']")?.getAttribute("href") ?? "",
    };

    const fullTitle = suffixBrand ? `${title} — ${BRAND.name}` : title;
    const finalDesc = description ?? DEFAULT_DESCRIPTION;
    const canonicalHref = `https://deft.computer${path ?? window.location.pathname}`;

    document.title = fullTitle;
    setMeta("meta[name='description']", finalDesc);
    setMeta("meta[property='og:title']", fullTitle);
    setMeta("meta[property='og:description']", finalDesc);
    setMeta("meta[name='twitter:title']", fullTitle);
    setMeta("meta[name='twitter:description']", finalDesc);
    setCanonical(canonicalHref);

    return () => {
      // Restore previous values so navigating back doesn't leave a stale title.
      document.title = prev.title;
      setMeta("meta[name='description']", prev.description);
      setMeta("meta[property='og:title']", prev.ogTitle);
      setMeta("meta[property='og:description']", prev.ogDesc);
      setMeta("meta[name='twitter:title']", prev.twTitle);
      setMeta("meta[name='twitter:description']", prev.twDesc);
      if (prev.canonical) setCanonical(prev.canonical);
    };
  }, [title, description, path, suffixBrand]);
}
