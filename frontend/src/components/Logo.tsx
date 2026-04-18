interface LogoProps {
  className?: string;
  size?: "sm" | "md" | "lg";
  showText?: boolean;
}

export function Logo({ className = "", size = "md", showText = true }: LogoProps) {
  const sizes = {
    sm: { icon: 20, text: "text-sm" },
    md: { icon: 24, text: "text-lg" },
    lg: { icon: 32, text: "text-2xl" },
  };

  const s = sizes[size];

  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <svg
        width={s.icon}
        height={s.icon}
        viewBox="0 0 32 32"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-label="Mariana"
      >
        <rect width="32" height="32" rx="6" className="fill-primary" />
        <path
          d="M7 22V12l5 6 5-6v10"
          stroke="white"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
        />
        <path
          d="M22 22V12"
          stroke="#26BDD6"
          strokeWidth="2.5"
          strokeLinecap="round"
        />
        <circle cx="22" cy="9" r="1.5" fill="#26BDD6" />
      </svg>
      {showText && (
        <span className={`${s.text} font-bold tracking-tight text-foreground`}>
          Mariana
        </span>
      )}
    </span>
  );
}
