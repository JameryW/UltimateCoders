/**
 * UltimateCoders — Interactive Script
 * Mobile navigation toggle, smooth scrolling, scroll-triggered animations,
 * active nav tracking, back-to-top, and status section interactivity.
 */

(function () {
    "use strict";

    // ─── DOM References ────────────────────────────────────────────────
    const header = document.getElementById("site-header");
    const primaryNav = document.getElementById("primary-nav");
    const navLinks = document.querySelectorAll(".nav-link, .footer-link");
    const sections = document.querySelectorAll(".content-section");
    const statusValues = document.querySelectorAll(".status-value");
    const featureCards = document.querySelectorAll(".feature-card");

    // ─── 1. Mobile Navigation Toggle ──────────────────────────────────
    // Dynamically inject a hamburger button for mobile viewports

    function createMobileToggle() {
        const navContainer = primaryNav;
        if (!navContainer) return;

        // Create hamburger button
        const toggleBtn = document.createElement("button");
        toggleBtn.className = "nav-toggle";
        toggleBtn.setAttribute("aria-label", "Toggle navigation menu");
        toggleBtn.setAttribute("aria-expanded", "false");
        toggleBtn.innerHTML = '<span class="hamburger-line"></span>' +
                              '<span class="hamburger-line"></span>' +
                              '<span class="hamburger-line"></span>';

        // Insert button before the nav list
        const headerContainer = header.querySelector(".header-container");
        if (headerContainer) {
            headerContainer.insertBefore(toggleBtn, navContainer);
        }

        // Toggle handler
        toggleBtn.addEventListener("click", function () {
            const isOpen = navContainer.classList.toggle("nav-open");
            toggleBtn.classList.toggle("toggle-active", isOpen);
            toggleBtn.setAttribute("aria-expanded", String(isOpen));
        });

        // Close nav when a link is clicked (mobile UX)
        navContainer.querySelectorAll("a").forEach(function (link) {
            link.addEventListener("click", function () {
                navContainer.classList.remove("nav-open");
                toggleBtn.classList.remove("toggle-active");
                toggleBtn.setAttribute("aria-expanded", "false");
            });
        });
    }

    // ─── 2. Smooth Scrolling ──────────────────────────────────────────
    // Intercept anchor clicks for smooth scroll with header offset

    function initSmoothScrolling() {
        document.querySelectorAll('a[href^="#"]').forEach(function (anchor) {
            anchor.addEventListener("click", function (e) {
                const targetId = this.getAttribute("href");
                if (targetId === "#") return;

                const targetEl = document.querySelector(targetId);
                if (!targetEl) return;

                e.preventDefault();

                const headerHeight = header ? header.offsetHeight : 0;
                const targetPosition = targetEl.getBoundingClientRect().top +
                                       window.pageYOffset -
                                       headerHeight -
                                       16; // 16px breathing room

                window.scrollTo({
                    top: targetPosition,
                    behavior: "smooth"
                });

                // Update URL hash without jumping
                if (history.pushState) {
                    history.pushState(null, null, targetId);
                }
            });
        });
    }

    // ─── 3. Active Navigation Tracking ────────────────────────────────
    // Highlight the nav link corresponding to the visible section

    function initActiveNavTracking() {
        if (!sections.length || !navLinks.length) return;

        const observerOptions = {
            root: null,
            rootMargin: "-20% 0px -60% 0px",
            threshold: 0
        };

        const observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    const id = entry.target.getAttribute("id");
                    navLinks.forEach(function (link) {
                        const href = link.getAttribute("href");
                        if (href === "#" + id) {
                            link.classList.add("active");
                        } else {
                            link.classList.remove("active");
                        }
                    });
                }
            });
        }, observerOptions);

        sections.forEach(function (section) {
            observer.observe(section);
        });
    }

    // ─── 4. Sticky Header with Shadow on Scroll ───────────────────────

    function initStickyHeader() {
        if (!header) return;

        let lastScroll = 0;

        window.addEventListener("scroll", function () {
            const currentScroll = window.pageYOffset;

            if (currentScroll > 10) {
                header.classList.add("header-scrolled");
            } else {
                header.classList.remove("header-scrolled");
            }

            lastScroll = currentScroll;
        }, { passive: true });
    }

    // ─── 5. Scroll-Reveal Animations ──────────────────────────────────
    // Animate elements into view as the user scrolls

    function initScrollReveal() {
        const revealElements = document.querySelectorAll(
            ".feature-card, .status-item, .section-title, .section-description, .hero-actions, .contact-info"
        );

        if (!revealElements.length) return;

        // Set initial hidden state
        revealElements.forEach(function (el) {
            el.classList.add("reveal-hidden");
        });

        const revealObserver = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    entry.target.classList.add("reveal-visible");
                    entry.target.classList.remove("reveal-hidden");
                    revealObserver.unobserve(entry.target);
                }
            });
        }, {
            root: null,
            rootMargin: "0px 0px -60px 0px",
            threshold: 0.1
        });

        revealElements.forEach(function (el) {
            revealObserver.observe(el);
        });
    }

    // ─── 6. Back-to-Top Button ────────────────────────────────────────

    function initBackToTop() {
        const btn = document.createElement("button");
        btn.className = "back-to-top";
        btn.setAttribute("aria-label", "Back to top");
        btn.innerHTML = "&#9650;"; // Up arrow
        document.body.appendChild(btn);

        window.addEventListener("scroll", function () {
            if (window.pageYOffset > 400) {
                btn.classList.add("back-to-top-visible");
            } else {
                btn.classList.remove("back-to-top-visible");
            }
        }, { passive: true });

        btn.addEventListener("click", function () {
            window.scrollTo({ top: 0, behavior: "smooth" });
        });
    }

    // ─── 7. Feature Card Hover Tilt Effect ────────────────────────────
    // Subtle 3D tilt on mouse move over feature cards

    function initCardTilt() {
        if (!featureCards.length) return;

        // Only enable on non-touch devices (hover-capable)
        if (window.matchMedia("(hover: none)").matches) return;

        featureCards.forEach(function (card) {
            card.addEventListener("mousemove", function (e) {
                const rect = card.getBoundingClientRect();
                const x = e.clientX - rect.left;
                const y = e.clientY - rect.top;
                const centerX = rect.width / 2;
                const centerY = rect.height / 2;

                const rotateX = ((y - centerY) / centerY) * -4;
                const rotateY = ((x - centerX) / centerX) * 4;

                card.style.transform =
                    "perspective(600px) rotateX(" + rotateX + "deg) rotateY(" + rotateY + "deg) scale(1.02)";
            });

            card.addEventListener("mouseleave", function () {
                card.style.transform = "perspective(600px) rotateX(0) rotateY(0) scale(1)";
            });
        });
    }

    // ─── 8. Status Section Pulse Animation ────────────────────────────
    // Simulate a live status check with animated pulse on status values

    function initStatusPulse() {
        if (!statusValues.length) return;

        // Simulate status data loading with a staggered reveal
        const statusData = [
            { id: "engine-status", value: "Operational", state: "ok" },
            { id: "workers-status", value: "3 / 4 Active", state: "ok" },
            { id: "circuit-breaker-status", value: "Closed", state: "ok" },
            { id: "scheduler-status", value: "Running", state: "ok" }
        ];

        statusData.forEach(function (item, index) {
            const el = document.getElementById(item.id);
            if (!el) return;

            setTimeout(function () {
                el.textContent = item.value;
                el.classList.add("status-" + item.state, "status-loaded");

                // Add a brief highlight flash
                el.classList.add("status-flash");
                setTimeout(function () {
                    el.classList.remove("status-flash");
                }, 600);
            }, 300 + index * 200);
        });
    }

    // ─── 9. Keyboard Accessibility ────────────────────────────────────
    // Allow Enter/Space to trigger interactive elements

    function initAccessibility() {
        // Make feature cards keyboard-focusable
        featureCards.forEach(function (card) {
            if (!card.getAttribute("tabindex")) {
                card.setAttribute("tabindex", "0");
            }
            card.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    card.classList.toggle("card-focused");
                }
            });
        });
    }

    // ─── 10. Current Year in Footer ───────────────────────────────────
    // Ensure copyright year is always current

    function initFooterYear() {
        const footerBrand = document.querySelector(".footer-brand p");
        if (footerBrand) {
            const currentYear = new Date().getFullYear();
            footerBrand.innerHTML = "&copy; " + currentYear + " UltimateCoders. All rights reserved.";
        }
    }

    // ─── 11. Page Load Animation ──────────────────────────────────────
    // Smooth fade-in on initial page load

    function initPageLoadAnimation() {
        document.body.classList.add("page-loaded");
    }

    // ─── Inject Dynamic Styles ────────────────────────────────────────
    // Minimal CSS that supports the JS-driven interactive features

    function injectStyles() {
        const style = document.createElement("style");
        style.textContent = [
            // Mobile nav toggle button
            ".nav-toggle {",
            "  display: none;",
            "  flex-direction: column;",
            "  justify-content: center;",
            "  gap: 5px;",
            "  background: none;",
            "  border: none;",
            "  cursor: pointer;",
            "  padding: 8px;",
            "  z-index: 100;",
            "}",

            ".hamburger-line {",
            "  display: block;",
            "  width: 24px;",
            "  height: 2px;",
            "  background-color: #e2e8f0;",
            "  border-radius: 2px;",
            "  transition: transform 0.3s ease, opacity 0.3s ease;",
            "}",

            ".toggle-active .hamburger-line:nth-child(1) {",
            "  transform: translateY(7px) rotate(45deg);",
            "}",

            ".toggle-active .hamburger-line:nth-child(2) {",
            "  opacity: 0;",
            "}",

            ".toggle-active .hamburger-line:nth-child(3) {",
            "  transform: translateY(-7px) rotate(-45deg);",
            "}",

            // Mobile nav overlay
            "@media (max-width: 768px) {",
            "  .nav-toggle { display: flex; }",
            "  #primary-nav {",
            "    display: none;",
            "    position: absolute;",
            "    top: 100%;",
            "    left: 0;",
            "    right: 0;",
            "    background: #0f172a;",
            "    border-bottom: 1px solid #1e293b;",
            "    padding: 16px 24px;",
            "    z-index: 99;",
            "  }",
            "  #primary-nav.nav-open { display: block; }",
            "  .nav-list { flex-direction: column; gap: 12px; }",
            "}",

            // Header scroll shadow
            ".header-scrolled {",
            "  box-shadow: 0 2px 20px rgba(0, 0, 0, 0.3);",
            "  transition: box-shadow 0.3s ease;",
            "}",

            // Active nav link
            ".nav-link.active, .footer-link.active {",
            "  color: #60a5fa !important;",
            "  position: relative;",
            "}",

            ".nav-link.active::after, .footer-link.active::after {",
            "  content: '';",
            "  position: absolute;",
            "  bottom: -4px;",
            "  left: 0;",
            "  right: 0;",
            "  height: 2px;",
            "  background-color: #60a5fa;",
            "  border-radius: 1px;",
            "}",

            // Scroll reveal animations
            ".reveal-hidden {",
            "  opacity: 0;",
            "  transform: translateY(24px);",
            "  transition: opacity 0.6s ease, transform 0.6s ease;",
            "}",

            ".reveal-visible {",
            "  opacity: 1;",
            "  transform: translateY(0);",
            "}",

            // Back-to-top button
            ".back-to-top {",
            "  position: fixed;",
            "  bottom: 24px;",
            "  right: 24px;",
            "  width: 44px;",
            "  height: 44px;",
            "  border-radius: 50%;",
            "  background: #1e293b;",
            "  border: 1px solid #334155;",
            "  color: #94a3b8;",
            "  font-size: 16px;",
            "  cursor: pointer;",
            "  opacity: 0;",
            "  transform: translateY(16px);",
            "  transition: opacity 0.3s ease, transform 0.3s ease, background 0.2s ease;",
            "  z-index: 50;",
            "  pointer-events: none;",
            "}",

            ".back-to-top-visible {",
            "  opacity: 1;",
            "  transform: translateY(0);",
            "  pointer-events: auto;",
            "}",

            ".back-to-top:hover {",
            "  background: #334155;",
            "  color: #e2e8f0;",
            "}",

            // Feature card tilt transition
            ".feature-card {",
            "  transition: transform 0.15s ease, box-shadow 0.3s ease;",
            "}",

            ".feature-card:hover {",
            "  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.25);",
            "}",

            ".card-focused {",
            "  outline: 2px solid #60a5fa;",
            "  outline-offset: 4px;",
            "}",

            // Status pulse animation
            ".status-loaded {",
            "  transition: color 0.3s ease;",
            "}",

            ".status-flash {",
            "  animation: statusPulse 0.6s ease;",
            "}",

            "@keyframes statusPulse {",
            "  0% { opacity: 0.3; transform: scale(0.95); }",
            "  50% { opacity: 1; transform: scale(1.05); }",
            "  100% { opacity: 1; transform: scale(1); }",
            "}",

            ".status-ok { color: #22c55e; }",
            ".status-degraded { color: #eab308; }",
            ".status-error { color: #ef4444; }",

            // Page load animation
            "body {",
            "  opacity: 0;",
            "  transition: opacity 0.4s ease;",
            "}",

            "body.page-loaded {",
            "  opacity: 1;",
            "}"
        ].join("\n");
        document.head.appendChild(style);
    }

    // ─── Initialize Everything ─────────────────────────────────────────

    function init() {
        injectStyles();
        createMobileToggle();
        initSmoothScrolling();
        initActiveNavTracking();
        initStickyHeader();
        initScrollReveal();
        initBackToTop();
        initCardTilt();
        initStatusPulse();
        initAccessibility();
        initFooterYear();
        initPageLoadAnimation();
    }

    // Run when DOM is ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

})();
