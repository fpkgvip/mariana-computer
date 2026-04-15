import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "@/contexts/AuthContext";

import Index from "./pages/Index";
import Research from "./pages/Research";
import Mariana from "./pages/Mariana";
import Chat from "./pages/Chat";
import Pricing from "./pages/Pricing";
import Contact from "./pages/Contact";
import Checkout from "./pages/Checkout";
import Account from "./pages/Account";
import Admin from "./pages/Admin";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import ResetPassword from "./pages/ResetPassword";
import Skills from "./pages/Skills";
import InvestigationGraph from "./pages/InvestigationGraph";
import NotFound from "./pages/NotFound";

const queryClient = new QueryClient();

/**
 * BUG-029: BrowserRouter now wraps AuthProvider so AuthProvider (and any
 * future consumers within it) can use React Router hooks (useNavigate etc.).
 *
 * BUG-010: Added /reset-password route so the password-reset email link
 * from Login.tsx actually lands on a page instead of the 404.
 */
const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <Toaster />
      <Sonner />
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/" element={<Index />} />
            <Route path="/research" element={<Research />} />
            <Route path="/mariana" element={<Mariana />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/pricing" element={<Pricing />} />
            <Route path="/contact" element={<Contact />} />
            <Route path="/checkout" element={<Checkout />} />
            {/* BUG-legacy: /buy-credits now redirects to /checkout for backward compat */}
            <Route path="/buy-credits" element={<Checkout />} />
            <Route path="/account" element={<Account />} />
            <Route path="/skills" element={<Skills />} />
            <Route path="/graph" element={<InvestigationGraph />} />
            <Route path="/graph/:taskId" element={<InvestigationGraph />} />
            <Route path="/admin" element={<Admin />} />
            <Route path="/login" element={<Login />} />
            <Route path="/signup" element={<Signup />} />
            <Route path="/reset-password" element={<ResetPassword />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
