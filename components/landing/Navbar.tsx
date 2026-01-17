"use client";

import { useState, useEffect } from "react";
import { Link } from "@/i18n/navigation";
import {
  SignedIn,
  SignedOut,
  SignInButton,
  SignUpButton,
  UserButton,
} from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/ThemeToggle";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { useTranslations } from "next-intl";
import { Menu, X } from "lucide-react";
import { cn } from "@/lib/utils";
import Image from 'next/image'

export function Navbar() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const t = useTranslations("nav");
  const tAuth = useTranslations("auth");

  useEffect(() => {
    const handleScroll = () => {
      setScrolled(window.scrollY > 10);
    };
    window.addEventListener("scroll", handleScroll);
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  const navLinks = [
    { href: "/#solution", label: t("solution") },
    { href: "/#technology", label: t("technology") },
    { href: "/#benefits", label: t("benefits") },
    { href: "/#markets", label: t("markets") },
    { href: "/faq", label: t("faq") },
  ];

  return (
    <header
      className={cn(
        "sticky top-0 z-50 w-full transition-all duration-200",
        scrolled
          ? "bg-background/80 backdrop-blur-md border-b"
          : "bg-transparent",
      )}
    >
      <nav className="container mx-auto px-4 h-16 flex items-center justify-between">
        {/* Logo */}
        <Link href="/" className="font-semibold text-xl tracking-tight flex flex-row">
           <Image
            src="/logo.png"
            width={30}
            height={30}
            alt="Anheart logo"
          />
          <div>AnHeart</div>
          
        </Link>

        {/* Desktop Navigation */}
        <div className="hidden md:flex items-center gap-8">
          {navLinks.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="text-sm text-muted-foreground hover:text-foreground transition-colors"
            >
              {link.label}
            </Link>
          ))}
        </div>

        {/* Right Side */}
        <div className="flex items-center gap-3">
          <ThemeToggle />
          <LanguageSwitcher />

          <SignedOut>
            <SignInButton mode="modal">
              <Button variant="ghost" size="sm" className="hidden sm:flex">
                {tAuth("signIn")}
              </Button>
            </SignInButton>
            <SignUpButton mode="modal">
              <Button size="sm">{tAuth("signUp")}</Button>
            </SignUpButton>
          </SignedOut>

          <SignedIn>
            <Link href="/dashboard" className="hidden sm:block">
              <Button variant="ghost" size="sm">
                {t("dashboard")}
              </Button>
            </Link>
            <UserButton afterSwitchSessionUrl="/" />
          </SignedIn>

          {/* Mobile Menu Button */}
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
          >
            {mobileMenuOpen ? (
              <X className="h-5 w-5" />
            ) : (
              <Menu className="h-5 w-5" />
            )}
          </Button>
        </div>
      </nav>

      {/* Mobile Menu */}
      <div
        className={cn(
          "md:hidden border-t bg-background overflow-hidden transition-all duration-200",
          mobileMenuOpen ? "max-h-80" : "max-h-0",
        )}
      >
        <div className="container mx-auto px-4 py-4 space-y-1">
          {navLinks.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="block py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
              onClick={() => setMobileMenuOpen(false)}
            >
              {link.label}
            </Link>
          ))}
          <SignedOut>
            <div className="pt-2 space-y-2">
              <SignInButton mode="modal">
                <button
                  className="block w-full py-2 text-sm text-left text-muted-foreground hover:text-foreground transition-colors"
                  onClick={() => setMobileMenuOpen(false)}
                >
                  {tAuth("signIn")}
                </button>
              </SignInButton>
              <SignUpButton mode="modal">
                <button
                  className="block w-full py-2 text-sm text-left text-primary hover:text-primary/80 transition-colors"
                  onClick={() => setMobileMenuOpen(false)}
                >
                  {tAuth("signUp")}
                </button>
              </SignUpButton>
            </div>
          </SignedOut>
          <SignedIn>
            <Link
              href="/dashboard"
              className="block py-2 text-sm text-primary"
              onClick={() => setMobileMenuOpen(false)}
            >
              {t("dashboard")}
            </Link>
          </SignedIn>
        </div>
      </div>
    </header>
  );
}
