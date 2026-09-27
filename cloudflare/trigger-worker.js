/**
 * Cloudflare Worker：每天準時叫 GitHub Actions 全站重抓。
 *
 * 為什麼要它：GitHub 自己的排程常延後一兩個小時（設 22:00，實際凌晨 1～3 點才跑），
 * Cloudflare 的 Cron Trigger 準時得多。它只做一件事：時間到了呼叫 GitHub 的
 * workflow_dispatch API，效果跟在 Actions 頁按「Run workflow → 全站重抓」一樣。
 *
 * 設定（Cloudflare 後台，不用裝任何東西）：
 *   1. Workers & Pages → Create → Worker，把這個檔案整份貼進去 → Deploy
 *   2. Settings → Variables and Secrets → 新增 Secret：
 *        GITHUB_TOKEN = GitHub 的 fine-grained token
 *        （只選 andy939/faq-crawler 這個 repo，權限 Actions: Read and write）
 *   3. Settings → Trigger Events → Cron Triggers → 新增 `0 23 * * *`
 *        （UTC 23:00 ＝ 臺北 07:00）
 *   4. 同一頁可以按「Trigger」先試一次，到 GitHub 的 Actions 頁看有沒有開始跑
 *
 * GitHub 那邊的 07:00 排程留著當備援：它開跑前會看今天是不是已經有完整的
 * 全站重抓，有就跳過（.github/workflows/crawl.yml），所以不會一天跑兩次。
 */

const REPO = "andy939/faq-crawler";
const WORKFLOW = "crawl.yml";
// 必須跟 crawl.yml 裡 workflow_dispatch 的選項一字不差，GitHub 會驗
const MODE = "全站重抓（約 1.5 小時）";

async function trigger(env) {
  if (!env.GITHUB_TOKEN) throw new Error("還沒設定 GITHUB_TOKEN（Settings → Variables and Secrets）");
  const r = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "faq-crawler-trigger",   // GitHub API 沒帶這個會拒絕
      },
      body: JSON.stringify({ ref: "main", inputs: { mode: MODE } }),
    });
  // 成功是 204、沒有內容。其他狀態把 GitHub 的說明原樣丟出來，
  // Cloudflare 後台的 Logs 看得到，才知道是 token 過期還是權限不夠
  if (r.status !== 204) throw new Error(`GitHub 回 ${r.status}：${await r.text()}`);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(trigger(env));
  },
  // 用瀏覽器打開這個 Worker 的網址只會看到說明，不會觸發 ——
  // 網址是公開的，不能讓任何人點一下就叫 GitHub 跑一輪
  async fetch() {
    return new Response(
      "faq-crawler 排程觸發器：每天 UTC 23:00（臺北 07:00）叫 GitHub Actions 全站重抓。\n",
      { headers: { "content-type": "text/plain; charset=utf-8" } });
  },
};
