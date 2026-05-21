function extractAnswer() {
  const selectors = [".prose", "[class*='prose']", "[class*='answer']"];
  const sourceSelectors = "[class*='source'], [class*='cit'], [class*='reference'], [class*='footnote'], sup";
  for (const sel of selectors) {
    const elements = document.querySelectorAll(sel);
    if (elements.length > 0) {
      return Array.from(elements)
        .map((el) => {
          const clone = el.cloneNode(true);
          clone.querySelectorAll(sourceSelectors).forEach((n) => n.remove());
          // Remove lists where every item contains a link — structural pattern for source lists
          clone.querySelectorAll("ol, ul").forEach((list) => {
            const items = Array.from(list.querySelectorAll("li"));
            if (items.length > 0 && items.every((li) => li.querySelector("a[href]"))) {
              list.remove();
            }
          });
          const host = document.createElement("div");
          host.style.cssText = "position:absolute;left:-9999px;top:-9999px";
          document.body.appendChild(host);
          host.appendChild(clone);
          const text = clone.innerText.trim();
          host.remove();
          return text;
        })
        .filter(Boolean)
        .join("\n\n");
    }
  }
  return null;
}

function showToast(message) {
  const existing = document.getElementById("pplx-copy-toast");
  if (existing) existing.remove();

  const toast = document.createElement("div");
  toast.id = "pplx-copy-toast";
  toast.textContent = message;
  Object.assign(toast.style, {
    position: "fixed",
    bottom: "24px",
    right: "24px",
    background: "#1a1a2e",
    color: "#fff",
    padding: "10px 18px",
    borderRadius: "8px",
    fontSize: "14px",
    fontFamily: "sans-serif",
    zIndex: "999999",
    boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
    transition: "opacity 0.4s ease",
    opacity: "1",
  });
  document.body.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 400);
  }, 2000);
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.action !== "copy-answer") return;

  const text = extractAnswer();
  if (!text) {
    showToast("✗ No answer found");
    return;
  }

  navigator.clipboard
    .writeText(text)
    .then(() => showToast("✓ Copied!"))
    .catch(() => showToast("✗ Clipboard access denied"));
});
