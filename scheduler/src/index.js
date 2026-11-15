// The clock and the "ears" of the showtime checker.
//
// GitHub's `schedule` trigger is best-effort (drifts, drops firings), so this Worker owns the timing and
// GitHub Actions stays the runner. Scraping deliberately does not move here: it needs curl_cffi's Chrome TLS
// impersonation to get past the cinemas' firewalls, which Workers' fetch() cannot do.
//
// Runs every minute:
//   1. Peeks at Telegram for messages / button taps nobody has answered yet. It only LOOKS (no offset is sent,
//      so nothing is marked as read); the bot itself reads and answers them during its run.
//      - button taps get an instant "⏳ got it" pop-up
//      - new messages get an instant "⏳ got it" reply
//      - then a check is started right away, so the real answer arrives in about a minute
//   2. Every 10th minute starts the regular check anyway.
// A check is never started while another one is already queued or running.
//
// Settings in the Cloudflare dashboard:
//   variables: GITHUB_REPO (OMAR6972/vox_cinema), GITHUB_REF (main)
//   secrets:   GITHUB_TOKEN (fine-grained, Actions read/write on this repo), TELEGRAM_BOT_TOKEN
//   cron:      * * * * *   (every minute; 1,440 runs a day, the free plan allows 100,000)

const WORKFLOW_FILE = "vox-alerts.yml";
const EVERY_MINUTES = 10;
const REPO = (env) => env.GITHUB_REPO || "OMAR6972/vox_cinema";
const REF = (env) => env.GITHUB_REF || "main";
const ACK_TEXT = "⏳ Got it! Give me about a minute, I'm on it.";

function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO(env)}/actions/${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "vox-cinema-scheduler", // GitHub rejects requests without one
      "Content-Type": "application/json",
    },
    redirect: "manual", // never replay the token to another host
  });
}

async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(`Telegram ${method}: ${j.description || r.status}`);
  return j.result;
}

async function busy(env) {
  for (const status of ["queued", "in_progress"]) {
    const r = await gh(env, `workflows/${WORKFLOW_FILE}/runs?status=${status}&per_page=1`);
    if (r.status !== 200) return false; // can't tell: better to start one than to stay silent
    const j = await r.json();
    if (j.total_count > 0) return true;
  }
  return false;
}

async function dispatch(env, why) {
  const r = await gh(env, `workflows/${WORKFLOW_FILE}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: REF(env) }),
  });
  if (r.status !== 204) throw new Error(`dispatch failed: ${r.status} ${await r.text()}`);
  console.log(`dispatched ${WORKFLOW_FILE} (${why})`);
}

async function waitingUpdates(env) {
  if (!env.TELEGRAM_BOT_TOKEN) return [];
  try {
    // No offset: just look, don't mark anything as read.
    return await tg(env, "getUpdates", { timeout: 0, limit: 100, allowed_updates: ["message", "callback_query"] });
  } catch (e) {
    console.log(`could not peek at Telegram: ${e.message}`);
    return [];
  }
}

async function acknowledge(env, updates, nowSec) {
  for (const u of updates) {
    try {
      if (u.callback_query) {
        // A second answer to the same tap is simply refused by Telegram, so repeats are harmless.
        await tg(env, "answerCallbackQuery", { callback_query_id: u.callback_query.id, text: ACK_TEXT });
      } else if (u.message && u.message.text && nowSec - u.message.date < 60) {
        // Only messages from the last minute, so each one is acknowledged once.
        await tg(env, "sendMessage", { chat_id: u.message.chat.id, text: ACK_TEXT });
      }
    } catch (e) {
      /* already answered / too old: fine */
    }
  }
}

export default {
  async scheduled(event, env) {
    if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN secret is missing in the Worker settings");
    const now = new Date(event.scheduledTime);
    const updates = await waitingUpdates(env);
    await acknowledge(env, updates, Math.floor(now.getTime() / 1000));

    const regular = now.getUTCMinutes() % EVERY_MINUTES === 0;
    if (!updates.length && !regular) return;
    if (await busy(env)) {
      console.log("a check is already queued or running; it (or the next minute) will pick this up");
      return;
    }
    await dispatch(env, updates.length ? `${updates.length} Telegram message(s) waiting` : "regular check");
  },
};
