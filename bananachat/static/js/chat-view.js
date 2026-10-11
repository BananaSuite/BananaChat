// Read-only conversations (shared links, administrator audit): render the
// Markdown of every element marked data-markdown and wire code-block buttons.
import { boot, t } from "./core.js";
import { enhanceMarkdown, renderMessage } from "./markdown.js";

const labels = {
  reasoning: t("chat_reasoning"),
  reasoningLive: t("chat_reasoning_live"),
  copy: t("copy"),
  copied: t("copied"),
  download: t("chat_download_code"),
  code: t("chat_code"),
};

for (const node of document.querySelectorAll("[data-markdown]")) {
  node.innerHTML = renderMessage(node.textContent, { labels });
  node.classList.add("is-rendered");
}
enhanceMarkdown(document, labels);

// Message times: the server writes UTC as a fallback; show them like the chat page does.
const dateFormat = new Intl.DateTimeFormat(boot.lang === "it" ? "it-IT" : "en-GB", { dateStyle: "medium", timeStyle: "short" });
// Only on the shared page: the administrator views stay in English with UTC times.
const localTimes = document.body.classList.contains("shared-chat-page") ? document.querySelectorAll(".message-meta time[datetime]") : [];
for (const node of localTimes) {
  const when = new Date(node.getAttribute("datetime"));
  if (Number.isNaN(when.getTime())) continue;
  node.title = node.textContent.trim();
  node.textContent = dateFormat.format(when);
}
