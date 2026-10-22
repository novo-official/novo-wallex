// The dashboard reads the Django read-only API (manage.py runserver 127.0.0.1:8000) through this proxy.
const api = process.env.API_URL || "http://127.0.0.1:8000";

export default {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${api}/api/:path*` }];
  },
};
