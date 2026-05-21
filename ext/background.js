chrome.commands.onCommand.addListener((command) => {
  if (command !== "copy-answer") return;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]?.id) {
      chrome.tabs.sendMessage(tabs[0].id, { action: "copy-answer" });
    }
  });
});
