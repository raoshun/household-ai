import type { NextConfig } from "next";

const backendOrigin = process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      // AG-UI 専用プロキシ（Browser -> Next -> Backend /agui）。
      // HttpAgent は SSE の Content-Type を strict に検査するため専用 route を使う。
      { source: "/backend-agui", destination: `${backendOrigin}/agui` },
      // 通常 REST API / SSE は frontend 側で绝对 URL（NEXT_PUBLIC_BACKEND_ORIGIN）で fetch する。
      // 開発中の切り分けのため /backend/* ホストプロキシも維持する（rewrites は dev/build 両方で有効）。
      { source: "/backend/", destination: backendOrigin },
      { source: "/backend/:path*", destination: `${backendOrigin}/:path*` },
    ];
  },
};

export default nextConfig;
