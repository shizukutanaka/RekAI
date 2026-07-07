/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // `standalone` produces a slim runtime for Docker but breaks `next start`,
  // so only enable it when explicitly requested (the web Dockerfile sets it).
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000",
  },
  async headers() {
    // `connect-src` must allow the API origin so browser fetches to it aren't
    // blocked — it's the same value baked into NEXT_PUBLIC_API_URL above, so
    // this always matches whatever backend the build was pointed at.
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    // 'unsafe-inline' on script-src is needed for Next.js's own inline
    // hydration/RSC payload scripts; there's no user-supplied HTML rendered
    // anywhere in this app (no dangerouslySetInnerHTML), so this isn't opening
    // a reflected-XSS hole the way it would if we rendered untrusted content.
    const csp = [
      "default-src 'self'",
      "script-src 'self' 'unsafe-inline'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data:",
      "font-src 'self'",
      `connect-src 'self' ${apiUrl}`,
      "frame-ancestors 'none'",
      "base-uri 'self'",
      "form-action 'self'",
    ].join("; ");
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Content-Security-Policy", value: csp },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
