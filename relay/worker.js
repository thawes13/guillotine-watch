// Relay between the public website and GitHub.
//
// The site's "Update now" button can't talk to GitHub directly, because that
// needs a login token and a public page can't keep one secret. This small
// service holds the token instead and does exactly one thing: ask GitHub to
// run the "Update site" job.
//
// It also runs on a timer, so updates no longer depend on GitHub's own
// (often late) scheduler.
//
// Abuse protection: it refuses to start a new run while one is in progress or
// if one started in the last MIN_GAP_SECONDS, no matter who asks.

const MIN_GAP_SECONDS = 120;

async function github(env, path, options = {}) {
  return fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "guillotine-relay",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });
}

// Returns { status: "started" | "running" | "recent" | "error", detail? }
async function requestUpdate(env) {
  const latest = await github(env, "/runs?per_page=1");
  if (!latest.ok) return { status: "error", detail: `GitHub said ${latest.status} when checking runs` };
  const run = (await latest.json()).workflow_runs?.[0];
  if (run) {
    if (run.status !== "completed") return { status: "running" };
    const ageSeconds = (Date.now() - new Date(run.created_at).getTime()) / 1000;
    if (ageSeconds < MIN_GAP_SECONDS) return { status: "recent" };
  }

  const started = await github(env, "/dispatches", { method: "POST", body: JSON.stringify({ ref: "main" }) });
  if (!started.ok) return { status: "error", detail: `GitHub said ${started.status} when starting a run` };
  return { status: "started" };
}

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

export default {
  // Called by the website's button.
  async fetch(request, env) {
    const headers = corsHeaders(env);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
    if (request.method !== "POST") return new Response("Guillotine Watch relay", { status: 200, headers });
    if (request.headers.get("Origin") !== env.ALLOWED_ORIGIN) {
      return Response.json({ status: "error", detail: "Not allowed from this site" }, { status: 403, headers });
    }
    const result = await requestUpdate(env);
    return Response.json(result, { status: result.status === "error" ? 502 : 200, headers });
  },

  // Called by Cloudflare's timer (see wrangler.toml).
  async scheduled(event, env, ctx) {
    ctx.waitUntil(requestUpdate(env));
  },
};
