

# Mobile Compatibility, Contact Page, Full Frontend Flows & Code Cleanup

## What's Changing

1. **Contact page** with fake contact info and working contact form
2. **Mobile responsiveness** fixes across all pages
3. **Complete frontend flows** — every button/link does something meaningful
4. **Code readability** improvements

---

## New Files

### `src/pages/Contact.tsx`
Contact page with:
- Fake contact details: phone `+1 (212) 555-0147`, emails `sales@mariana.co`, `support@mariana.co`, `legal@mariana.co`
- Office address (fake NYC address)
- Working contact form (name, email, subject dropdown, message) — submits to local state, shows toast confirmation
- Same premium layout as other pages (Navbar, Footer, ScrollReveal)

### `src/pages/BuyCredits.tsx`
Simple credit purchase page:
- Token amount selector (preset amounts: $10, $25, $50, $100, custom)
- Fake card payment form (card number, expiry, CVC) — frontend only
- Shows confirmation toast on "purchase", adds tokens via `addTokens()` from AuthContext
- Requires login (redirects to `/login` if not authenticated)

### `src/pages/Account.tsx`
Basic account settings page:
- Shows user name, email, token balance
- "Buy Credits" link to `/buy-credits`
- "Sign Out" button
- Requires login

---

## Modified Files

### `src/App.tsx`
- Add routes: `/contact`, `/buy-credits`, `/account`
- Import new page components

### `src/components/Navbar.tsx`
- Add "Contact" to nav links
- When logged in, user dropdown adds "Account" and "Buy Credits" links
- Mobile menu: fix spacing, add Contact link, add Account/Buy Credits when logged in

### `src/components/Footer.tsx`
- Add Contact link
- Add `support@mariana.co` email
- Slightly more structured footer with column layout on desktop

### `src/pages/Index.tsx`
- Mobile fixes: reduce hero text size on small screens (already has `sm:text-6xl` but check `text-5xl` base is not too big on 320px)
- Ensure grid sections stack properly on mobile
- Terminal card: ensure no horizontal overflow on small screens

### `src/pages/Chat.tsx`
- Mobile: show a hamburger/menu button to toggle sidebar visibility
- Selectors: stack vertically on very small screens
- Input area: ensure it doesn't overflow on mobile

### `src/pages/Pricing.tsx`
- Cards: use `grid-cols-1 md:grid-cols-2 lg:grid-cols-3` for proper stacking
- "Contact sales" button on Custom plan links to `/contact`
- Enterprise "Get started" links to `/signup`
- Add "Buy Credits" link somewhere for existing users

### `src/pages/Research.tsx`
- Filters: make sector pills horizontally scrollable on mobile instead of wrapping messily
- Report cards: reduce padding on mobile

### `src/pages/Mariana.tsx`
- Research depth cards: `grid-cols-1 sm:grid-cols-2 lg:grid-cols-4`
- Use cases grid: `grid-cols-1 md:grid-cols-3`

### `src/pages/Login.tsx` & `src/pages/Signup.tsx`
- Add "Forgot password?" link (just shows a toast saying "Password reset would be emailed" since no backend)
- Ensure form works on small screens

### `src/pages/NotFound.tsx`
- Light mode styling (currently uses `bg-muted`, update to match site)
- Add Navbar/Footer or at least proper link styling

### `src/contexts/AuthContext.tsx`
- Add `buyCredits` method (alias for `addTokens` but semantically clearer)
- Persist user to `localStorage` so refresh doesn't log out (frontend-only convenience)

---

## Code Readability Improvements

- Extract repeated inline class strings for inputs into a shared constant or use the existing `Input` component from `src/components/ui/input.tsx` in Login, Signup, Contact, Chat forms
- Extract repeated CTA button patterns into a small helper or consistently use the `Button` component
- Add brief JSDoc comments to AuthContext exports
- Group imports consistently (react, router, components, contexts, icons)
- Clean up any unused imports

---

## Mobile-Specific Fixes (cross-cutting)

- All hero headings: `text-3xl sm:text-4xl md:text-5xl lg:text-[bigger]` progression
- All section padding: `py-16 md:py-24` instead of `py-24 md:py-32` on mobile
- All grids: proper `grid-cols-1` base
- Chat sidebar: hidden on mobile with toggle button
- Navbar mobile menu: ensure all links present and functional
- Forms: full-width on mobile with proper spacing
- Terminal/code blocks: `overflow-x-auto` to prevent horizontal scroll

---

## Technical Notes

- No new dependencies needed
- All forms submit to local state + toast notifications
- BuyCredits uses `addTokens` from AuthContext — no real payment processing
- localStorage persistence is a convenience; will be replaced by real auth later
- Contact form just shows a success toast — no email sending

