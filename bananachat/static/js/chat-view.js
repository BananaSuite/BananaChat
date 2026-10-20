// Read-only conversations (shared links, administrator audit): render the
// Markdown of every element marked data-markdown and wire code-block buttons.
import { t } from "./core.js";
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
