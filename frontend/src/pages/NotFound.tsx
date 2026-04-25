import { Link } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { usePageHead } from "@/lib/pageHead";

/**
 * 404 — calm operator voice.  No "Oops", no exclamation marks, no
 * apologies for missing pages we never wrote.  Just a clear statement
 * and a way back.
 */
const NotFound = () => {
  usePageHead({ title: "Page not found", description: "That page does not exist." });

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <div className="flex min-h-[70vh] flex-col items-center justify-center px-6 text-center">
        <p className="font-mono text-[12px] tracking-[0.04em] text-muted-foreground">404</p>
        <h1 className="mt-3 text-balance text-3xl font-semibold tracking-[-0.02em] text-foreground sm:text-4xl">
          That page doesn{"\u2019"}t exist.
        </h1>
        <p className="mt-4 max-w-md text-[14px] leading-[1.6] text-muted-foreground">
          The URL is wrong, the link is stale, or the page was moved.
          Head back to the home page and try again.
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Link
            to="/"
            className="rounded-md bg-accent px-5 py-2.5 text-[14px] font-medium text-accent-foreground transition-all hover:brightness-110"
          >
            Back to home
          </Link>
          <Link
            to="/build"
            className="text-[14px] font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            Open the studio →
          </Link>
        </div>
      </div>
      <Footer />
    </div>
  );
};

export default NotFound;
